"""
提示词快照：证明一次重构没有改变模型面。

在离线 Mock 模型下跑一组固定的问题，按调用顺序记下每个"面向模型"的协作者收到了什么：

- 路由、抽词、检索、规划、SQL 生成、修复、结果校验、对话问答的输入；
- 两个模型客户端真正拼出来的提示词（长文本记 sha256 + 长度）；
- 每一条被执行的 SQL。

重构（拆 `Text2SQLPipeline._run`、加运行日志、加取消点）之后，这份记录必须与
`tests/fixtures/prompt_snapshot.json` 逐项相同。相同，就说明首次运行时每一次模型调用的
输入和顺序都没变，这次重构不需要跑模型门禁。

只有**有意**改变模型面（改提示词、改调用顺序）时才重录，重录之后照规矩跑模型回归：

    UPDATE_PROMPT_SNAPSHOT=1 python -m pytest tests/test_prompt_snapshot.py

离线 Mock 下路由、对话问答、语义自检走的是规则版，模型版的提示词不在这份快照里；
它们的提示词由各自模块拼装，输入则由这里记录的 `route` / `answer` / `validate` 参数覆盖。

模型面还必须与字符串哈希种子无关：换一个种子等于重启一次进程，同一个问题的提示词得一字不差，
否则复现不了，前缀缓存也命不中。`PromptsIgnoreHashSeedTest` 在两个种子下各录一遍全文比对。
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Callable, List, Sequence
from unittest import mock

from ai_sql_agent.core import get_settings

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "prompt_snapshot.json"

# 单表、多表 join、跨库两步、执行失败（会进修复阶段）、再加一条按规则路由到对话问答的追问。
QUESTIONS = (
    "查询准时率低于0.9的供应商名称",
    "查询库存低于安全库存的物料名称",
    "查询实际成本超过预算的各个项目的采购总金额",
    "查询采购状态为ordered的采购单号",
    "解释一下刚才的查询结果",
)

# 跨种子比对再加一个带两个等长枚举取值的问题：规则抽词时「华鹏板材」「东顺五金」谁在前，
# 曾经取决于集合的遍历顺序。
HASH_SEED_QUESTIONS = QUESTIONS + ("查询华鹏板材和东顺五金的采购单号",)

FROZEN_AS_OF = "2026-01-01T00:00:00Z"

_INLINE_LIMIT = 80


def _digest(text: str, full_text: bool = False) -> Any:
    if full_text or len(text) <= _INLINE_LIMIT:
        return text
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], "len": len(text)}


def _norm(value: Any, full_text: bool = False) -> Any:
    """把参数变成可比较、不随运行变化的 JSON。长文本只留指纹，full_text 时留全文。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _digest(value, full_text)
    if isinstance(value, (list, tuple)):
        return [_norm(item, full_text) for item in value]
    if isinstance(value, dict):
        return {str(key): _norm(value[key], full_text) for key in sorted(value, key=str)}
    to_prompt = getattr(value, "to_prompt_context", None)
    if callable(to_prompt):
        return {"__type__": type(value).__name__, "prompt": _digest(to_prompt(), full_text)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            field.name: _norm(getattr(value, field.name), full_text)
            for field in dataclasses.fields(value)
        }
        return {"__type__": type(value).__name__, **fields}
    if callable(value):
        return "<callable>"
    return f"<{type(value).__name__}>"


def _targets():
    """(类, 方法名, 记录参数时丢掉的形参) —— 都是模型面上的协作者。"""
    from ai_sql_agent.execution.router import MCPRouter
    from ai_sql_agent.generation.coder_client import CoderModelClient
    from ai_sql_agent.generation.sql_generator import SqlGenerator
    from ai_sql_agent.pipeline.data_qa import ModelDataQAClient, RuleBasedDataQAClient
    from ai_sql_agent.pipeline.local_clients import HybridKeywordExtractor
    from ai_sql_agent.pipeline.routing import DynamicIntentRouter
    from ai_sql_agent.planning.cot_planner import CotPlanner
    from ai_sql_agent.planning.thinking_client import ThinkingModelClient
    from ai_sql_agent.repair.repairer import SqlRepairService
    from ai_sql_agent.retrieval.hierarchical_service import ThreeTierSchemaRetrievalService
    from ai_sql_agent.validation.validator import ResultValidator

    return (
        (DynamicIntentRouter, "route", ()),
        (HybridKeywordExtractor, "extract", ()),
        (ThreeTierSchemaRetrievalService, "retrieve", ("access",)),
        (CotPlanner, "plan", ()),
        (ThinkingModelClient, "generate", ()),
        (ThinkingModelClient, "generate_structured", ()),
        (ThinkingModelClient, "generate_stream", ()),
        (SqlGenerator, "generate", ()),
        (CoderModelClient, "generate_sql", ()),
        (SqlRepairService, "repair", ("execute",)),
        (ResultValidator, "validate", ()),
        (RuleBasedDataQAClient, "answer", ()),
        (ModelDataQAClient, "answer", ()),
        (MCPRouter, "execute", ()),
    )


def _recorder(
    records: List[dict], owner: type, name: str, drop: tuple, full_text: bool
) -> Callable:
    original = getattr(owner, name)

    def wrapper(self, *args, **kwargs):
        kept = {key: value for key, value in kwargs.items() if key not in drop}
        records.append({
            "call": f"{owner.__name__}.{name}",
            "args": _norm(list(args), full_text),
            "kwargs": _norm(kept, full_text),
        })
        return original(self, *args, **kwargs)

    return wrapper


def record_snapshot(
    questions: Sequence[str] = QUESTIONS, full_text: bool = False
) -> List[dict]:
    """在 Mock 模型下跑一组问题（默认 QUESTIONS），返回按调用顺序的记录。"""
    from ai_sql_agent.memory import ConversationMemoryService
    from ai_sql_agent.memory.conversation import MemoryServiceConfig
    from ai_sql_agent.memory.short_term import ShortTermMemoryConfig
    from ai_sql_agent.pipeline import Text2SQLPipeline
    from ai_sql_agent.pipeline.dynamic_service import DynamicSqlAgentService

    records: List[dict] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=Path(temp_dir) / "memory.db",
                embedding_dimensions=128,
                use_model_summarizer_when_available=False,
                use_reranker_when_available=False,
                vector_backend="none",
                # 摘要同步做：后台线程什么时候做完是竞态，会话上下文就会忽有忽无一段摘要
                short_term=ShortTermMemoryConfig(async_summary=False),
            )
        )
        try:
            service = DynamicSqlAgentService(Text2SQLPipeline(), memory)
            patches = [
                mock.patch.object(owner, name, autospec=True,
                                  side_effect=_recorder(records, owner, name, drop, full_text))
                for owner, name, drop in _targets()
            ]
            patches.append(mock.patch(
                "ai_sql_agent.execution.sqlite_executor.now_rfc3339", return_value=FROZEN_AS_OF
            ))
            for patcher in patches:
                patcher.start()
            try:
                session = f"snap_{uuid.uuid4().hex[:8]}"
                for question in questions:
                    records.append({"question": question})
                    result = service.run(user_id="u_snapshot", session_id=session, query=question)
                    records.append({
                        "route": result.decision.route.value,
                        "answer": _digest(result.answer, full_text),
                        "degraded": result.degraded,
                    })
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
        finally:
            memory.close(wait=True)
    return records


_CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
from ai_sql_agent.core import reload_settings
from ai_sql_agent.core.settings import SqlAgentSettings
SqlAgentSettings.model_config["env_file"] = None
reload_settings()
import test_prompt_snapshot as snapshot
if sys.argv[3] == "full_text":
    result = snapshot.record_snapshot(snapshot.HASH_SEED_QUESTIONS, full_text=True)
else:
    result = [snapshot.record_snapshot(), snapshot.record_snapshot()]
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False)
"""


def _recording_hash_seed() -> str:
    """
    录快照用的字符串哈希种子：环境里设了 PYTHONHASHSEED 就用它，否则随机挑一个。

    以前钉死在 0：Schema 图里表的先后、规则抽词时等长枚举取值的先后都随种子变，
    不钉死就没有基线。这两处现在都排好了序，快照在哪个种子下都该成立，随机挑一个
    等于每跑一次顺带抽查一个种子。失败信息里带着它，设同样的 PYTHONHASHSEED 再跑就能复现。
    """
    return os.environ.get("PYTHONHASHSEED") or str(secrets.randbelow(2 ** 32))


def record_in_subprocess(hash_seed: str, mode: str = "snapshot") -> Any:
    """
    在 PYTHONHASHSEED=hash_seed 的子进程里录。

    - mode="snapshot"：QUESTIONS 录两遍，长文本只留指纹，返回两份记录；
    - mode="full_text"：HASH_SEED_QUESTIONS 录一遍，长文本留全文，返回一份记录。

    哈希种子只能在进程启动前定，所以要子进程。子进程用和当前进程相同的 sys.path，
    所以测的是同一份代码；工作目录是临时目录，演示库建在那里，不碰仓库里的 runtime_data。
    """
    import subprocess
    import sys

    with tempfile.TemporaryDirectory() as work_dir:
        out_path = Path(work_dir) / "records.json"
        env = dict(os.environ)
        env.update({
            "PYTHONHASHSEED": hash_seed,
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "AI_SQL_LOG_LEVEL": "ERROR",
            "AI_SQL_MODEL_SETTINGS_ENABLED": "false",
        })
        for key in ("AI_SQL_OPENAI_API_KEY", "OPENAI_API_KEY",
                    "AI_SQL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
            env[key] = ""
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, str(Path(__file__).parent), str(out_path), mode],
            cwd=work_dir, env=env, capture_output=True, text=True, timeout=300,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"录制子进程失败（PYTHONHASHSEED={hash_seed}）：\n{completed.stderr[-4000:]}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


def _leaves(value: Any, path: str = "") -> dict:
    """把一条记录摊平成 {路径: 叶子值}。"""
    if isinstance(value, dict):
        children = value.items()
    elif isinstance(value, list):
        children = enumerate(value)
    else:
        return {path: value}
    leaves: dict = {}
    for key, child in children:
        leaves.update(_leaves(child, f"{path}/{key}"))
    return leaves


def _explain_difference(left: Any, right: Any) -> str:
    """两条记录哪几处不同。长文本给逐行 diff，看得出是哪几行挪了位置。"""
    left_leaves, right_leaves = _leaves(left), _leaves(right)
    lines: List[str] = []
    for path in sorted(set(left_leaves) | set(right_leaves)):
        before, after = left_leaves.get(path), right_leaves.get(path)
        if before == after:
            continue
        if isinstance(before, str) and isinstance(after, str):
            diff = difflib.unified_diff(
                before.splitlines(), after.splitlines(), lineterm="", n=1
            )
            lines.append(f"{path}:")
            lines.extend(list(diff)[2:])  # 去掉 ---/+++ 两行文件头
        else:
            lines.append(f"{path}: {before!r} != {after!r}")
    return "\n".join(lines)


class PromptSnapshotTest(unittest.TestCase):
    """首次运行的模型面与录下的快照逐项相同。"""

    @classmethod
    def setUpClass(cls):
        cls.runs = None
        cls.hash_seed = _recording_hash_seed()
        if not get_settings().has_model_credentials:
            cls.runs = record_in_subprocess(cls.hash_seed)

    def setUp(self):
        if self.runs is None:
            self.skipTest("快照只在离线 Mock 模型下有意义；当前环境配置了模型凭证")

    def test_model_facing_calls_match_the_recorded_snapshot(self):
        records = self.runs[0]

        if os.environ.get("UPDATE_PROMPT_SNAPSHOT") == "1":
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT_PATH.write_text(
                json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            self.skipTest(f"已重录 {SNAPSHOT_PATH.name}（{len(records)} 条）")

        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        for index, (got, want) in enumerate(zip(records, expected)):
            self.assertEqual(
                got, want,
                f"第 {index} 条记录与快照不同（录制用的 PYTHONHASHSEED={self.hash_seed}）"
                "——模型面变了。有意变更才重录（见模块说明）。",
            )
        self.assertEqual(len(records), len(expected), "记录条数与快照不同")

    def test_two_runs_record_the_same_thing(self):
        """快照本身必须稳定，否则它证明不了任何事。"""
        self.assertEqual(self.runs[0], self.runs[1], f"PYTHONHASHSEED={self.hash_seed}")


class PromptsIgnoreHashSeedTest(unittest.TestCase):
    """
    换一个字符串哈希种子（等于重启一次进程），模型收到的每一段输入都一字不差。

    曾经不是这样：Schema 图里表的先后来自一个字符串集合，规则抽词时等长枚举取值的先后
    也来自集合，两处都随 PYTHONHASHSEED 变。这里在两个固定种子下各录一遍全文（不取指纹）
    逐条比对；修复之前，这两个种子录出来的 Schema 上下文和抽词顺序都不一样。
    """

    SEEDS = ("1", "2")

    def setUp(self):
        if get_settings().has_model_credentials:
            self.skipTest("只在离线 Mock 模型下有意义；当前环境配置了模型凭证")

    def test_two_hash_seeds_give_the_model_the_same_inputs(self):
        first = record_in_subprocess(self.SEEDS[0], mode="full_text")
        second = record_in_subprocess(self.SEEDS[1], mode="full_text")
        for index, (left, right) in enumerate(zip(first, second)):
            if left != right:
                label = left.get("call") or left.get("question") or "问题的结果"
                self.fail(
                    f"第 {index} 条记录（{label}）在 PYTHONHASHSEED={self.SEEDS[0]} 与 "
                    f"{self.SEEDS[1]} 下不同——模型面又跟着字符串哈希走了：\n"
                    + _explain_difference(left, right)
                )
        self.assertEqual(len(first), len(second), "两个种子下的记录条数不同")


if __name__ == "__main__":
    unittest.main()
