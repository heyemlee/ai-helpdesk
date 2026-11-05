"""
对话记忆库的位置由配置决定（`AI_SQL_MEMORY_DB_PATH`）。

这个配置项一直在 Settings 里，却没有任何代码读它：服务启动时写死用
`./runtime_data/memory.db`。同一个工作目录下起两个实例，对话历史就写进同一个文件——
在一个实例里问过的问题，会被另一个实例当成「刚才的查询结果」来回答。

锁三件事：
1. 配了路径就用那个路径，默认路径不会被顺手建出来；
2. 两个实例各用各的库，同一个用户、同一个会话号也互相看不到；
3. 已有的记忆库照原样打开：不清空、不迁移。默认值与原来写死的路径相同。
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ai_sql_agent.core import reload_settings
from ai_sql_agent.memory import (
    ConversationMemoryService,
    MemoryServiceConfig,
    MessageRole,
    create_memory_service,
)
from ai_sql_agent.memory.conversation import DEFAULT_MEMORY_DB_PATH

ENV_KEYS = (
    "AI_SQL_MEMORY_DB_PATH", "AI_SQL_AUDIT_DB_PATH", "AI_SQL_AUTH_MODE", "AI_SQL_SEMANTIC_DIR",
)


class _IsolatedEnvironment(unittest.TestCase):
    """每个用例在自己的临时目录里跑：工作目录、环境变量用完都还原。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmpdir.name).resolve()
        self._cwd = os.getcwd()
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        os.chdir(self.directory)
        os.environ.update({"AI_SQL_AUTH_MODE": "disabled", "AI_SQL_SEMANTIC_DIR": ""})
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        self._tmpdir.cleanup()

    def configure(self, **values):
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        reload_settings()


def _messages(path: Path):
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT content FROM memory_messages ORDER BY id")]


class MemoryPathSettingTest(_IsolatedEnvironment):
    def test_the_configured_path_is_used_and_the_default_is_not_created(self):
        target = self.directory / "elsewhere" / "practice-memory.db"
        self.configure(AI_SQL_MEMORY_DB_PATH=target)

        service = create_memory_service()
        try:
            self.assertEqual(service.store.db_path, target)
            self.assertTrue(target.exists())
            self.assertFalse((self.directory / DEFAULT_MEMORY_DB_PATH).exists())
        finally:
            service.close()

    def test_unset_or_blank_keeps_the_path_existing_deployments_use(self):
        # 默认值必须和原来写死的那个路径一样：否则升级之后服务读到的是一份空库，
        # 看起来就像记忆全丢了
        self.assertEqual(DEFAULT_MEMORY_DB_PATH, "runtime_data/memory.db")
        self.assertEqual(MemoryServiceConfig().db_path, DEFAULT_MEMORY_DB_PATH)
        for value in (None, "", "   "):
            self.configure(AI_SQL_MEMORY_DB_PATH=value)
            service = create_memory_service()
            try:
                self.assertEqual(service.store.db_path, Path(DEFAULT_MEMORY_DB_PATH), repr(value))
            finally:
                service.close()
        self.assertTrue((self.directory / DEFAULT_MEMORY_DB_PATH).exists())

    def test_an_existing_memory_file_is_opened_as_it_is(self):
        target = self.directory / "memory.db"
        before = ConversationMemoryService(MemoryServiceConfig(
            db_path=target, use_model_summarizer_when_available=False, use_reranker_when_available=False,
        ))
        before.begin_user_turn(user_id="u1", session_id="s1", query="上周问过的问题")
        before.close()
        size = target.stat().st_size

        self.configure(AI_SQL_MEMORY_DB_PATH=target)
        service = create_memory_service()
        try:
            kept = service.store.list_messages(user_id="u1", session_id="s1")
            self.assertEqual([message.content for message in kept], ["上周问过的问题"])
            self.assertEqual(kept[0].role, MessageRole.USER)
            self.assertGreaterEqual(target.stat().st_size, size)
        finally:
            service.close()


class TwoInstancesTest(_IsolatedEnvironment):
    """同一个工作目录、同一个用户、同一个会话号，两个实例——只有记忆库路径不同。"""

    def _run_instance(self, name: str, question: str):
        from ai_sql_agent.api.app import app

        memory_path = self.directory / f"{name}-memory.db"
        self.configure(
            AI_SQL_MEMORY_DB_PATH=memory_path,
            AI_SQL_AUDIT_DB_PATH=self.directory / f"{name}-audit.db",
        )
        with TestClient(app) as client:
            self.assertEqual(app.state.memory.store.db_path, memory_path)
            seen_before = [
                message.content
                for message in app.state.memory.store.list_messages(user_id="u1", session_id="s1")
            ]
            response = client.post(
                "/query", json={"user_id": "u1", "session_id": "s1", "query": question}
            )
            self.assertEqual(response.status_code, 200, response.text)
        return memory_path, seen_before

    def test_history_does_not_leak_between_instances(self):
        first_question = "查询准时率低于0.9的供应商名称"
        second_question = "查询采购状态为ordered的采购单号"

        first_path, first_seen = self._run_instance("first", first_question)
        second_path, second_seen = self._run_instance("second", second_question)

        # 第二个实例启动时看不到第一个实例里的任何对话
        self.assertEqual(first_seen, [])
        self.assertEqual(second_seen, [])
        first_messages = _messages(first_path)
        second_messages = _messages(second_path)
        self.assertIn(first_question, first_messages)
        self.assertNotIn(second_question, first_messages)
        self.assertIn(second_question, second_messages)
        self.assertNotIn(first_question, second_messages)
        # 两边都没有往默认位置写
        self.assertFalse((self.directory / DEFAULT_MEMORY_DB_PATH).exists())

    def test_restarting_an_instance_finds_its_own_history_again(self):
        question = "查询准时率低于0.9的供应商名称"
        path, _ = self._run_instance("first", question)
        again_path, seen = self._run_instance("first", "查询采购状态为ordered的采购单号")
        self.assertEqual(again_path, path)
        self.assertIn(question, seen)


if __name__ == "__main__":
    unittest.main()
