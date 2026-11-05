from __future__ import annotations

import copy
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ai_sql_agent.execution import MCPExecutionResult
from ai_sql_agent.memory import (
    ConversationMemoryService,
    ExtractiveMemorySummarizer,
    LocalMemoryEmbeddingClient,
    MemoryKind,
    MemoryMessage,
    MemoryServiceConfig,
    MessageRole,
    ModelMemorySummarizer,
    ShortTermMemoryConfig,
    ShortTermMemoryService,
    SQLiteMemoryStore,
    SummaryBudgetConfig,
)
from ai_sql_agent.memory.long_term import LongTermMemoryService
from ai_sql_agent.pipeline import (
    Text2SQLPipeline,
    MemoryAwareSqlAgentService,
    PipelineConfig,
    PipelineResult,
    RuleBasedDataQAClient,
    StepExecutionLog,
)


class BlockingSummarizer(ExtractiveMemorySummarizer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def summarize_conversation(self, previous_summary, messages):
        self.started.set()
        self.release.wait(timeout=5)
        return super().summarize_conversation(previous_summary, messages)


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_async_summary_never_blocks_context_read(self) -> None:
        store = SQLiteMemoryStore(self.db_path)
        summarizer = BlockingSummarizer()
        service = ShortTermMemoryService(
            store=store,
            summarizer=summarizer,
            config=ShortTermMemoryConfig(
                max_window_messages=2,
                max_window_tokens=1000,
                async_summary=True,
            ),
        )
        try:
            service.add_message(user_id="u1", session_id="s1", role="user", content="第一问")
            service.add_message(user_id="u1", session_id="s1", role="assistant", content="第一答")
            service.add_message(user_id="u1", session_id="s1", role="user", content="第二问")
            self.assertTrue(summarizer.started.wait(timeout=2))

            context_while_running = service.get_context(user_id="u1", session_id="s1")
            self.assertTrue(context_while_running.summary_pending)
            self.assertIsNone(context_while_running.summary)
            self.assertEqual([m.content for m in context_while_running.messages], ["第一答", "第二问"])

            summarizer.release.set()
            service.wait_for_summary(user_id="u1", session_id="s1", timeout=5)
            completed = service.get_context(user_id="u1", session_id="s1")
            self.assertIn("第一问", completed.summary.content)
            self.assertEqual(completed.summary.through_message_id, 1)
        finally:
            summarizer.release.set()
            service.close()

    def test_token_limit_keeps_latest_message(self) -> None:
        store = SQLiteMemoryStore(self.db_path)
        service = ShortTermMemoryService(
            store=store,
            config=ShortTermMemoryConfig(
                max_window_messages=10,
                max_window_tokens=2,
                async_summary=False,
            ),
        )
        try:
            service.add_message(user_id="u1", session_id="s1", role=MessageRole.USER, content="很长的一条消息")
            service.add_message(user_id="u1", session_id="s1", role=MessageRole.ASSISTANT, content="最新消息")
            context = service.get_context(user_id="u1", session_id="s1")
            self.assertEqual([message.content for message in context.messages], ["最新消息"])
            self.assertIsNotNone(context.summary)
        finally:
            service.close()

    def test_long_term_recall_is_user_isolated_and_filterable(self) -> None:
        store = SQLiteMemoryStore(self.db_path)
        service = LongTermMemoryService(
            store=store,
            embedding_client=LocalMemoryEmbeddingClient(dimensions=128),
        )
        trade = service.save_structured(
            user_id="u1",
            query="查询供应商名称",
            result={"supplier_name": "烤漆"},
            database="erp_db",
            tables=["supplier"],
        )
        service.save_unstructured(
            user_id="u1",
            title="客服口径",
            content="退款审核通常需要三个工作日",
        )
        service.save_unstructured(
            user_id="u2",
            title="私有信息",
            content="供应商名称是内部保密信息",
        )

        hits = service.recall(user_id="u1", query="之前查询的供应商", top_k=2)
        self.assertEqual(hits[0].memory.id, trade.id)
        self.assertTrue(all(hit.memory.user_id == "u1" for hit in hits))

        filtered = service.recall(
            user_id="u1",
            query="门板",
            top_k=5,
            kinds=[MemoryKind.STRUCTURED],
            metadata_filters={"database": "erp_db", "tables": ["supplier"]},
        )
        self.assertEqual([hit.memory.id for hit in filtered], [trade.id])

    def test_long_term_is_only_written_by_explicit_save(self) -> None:
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=self.db_path,
                embedding_dimensions=128,
                vector_backend="none",
                use_model_summarizer_when_available=False,
                short_term=ShortTermMemoryConfig(max_window_messages=4),
            )
        )
        try:
            memory.begin_user_turn(user_id="u1", session_id="s1", query="解释这个结果")
            memory.record_assistant_message(
                user_id="u1", session_id="s1", content="这是结果解释"
            )
            self.assertEqual(memory.store.list_long_term_memories(user_id="u1"), [])

            saved = memory.save_unstructured_content(
                user_id="u1", title="结果解释", content="这是结果解释"
            )
            self.assertEqual(
                memory.store.get_long_term_memory(user_id="u1", memory_id=saved.id).id,
                saved.id,
            )
        finally:
            memory.close()

    def test_memory_aware_pipeline_records_turn_and_supports_explicit_save(self) -> None:
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=self.db_path,
                embedding_dimensions=128,
                vector_backend="none",
                use_model_summarizer_when_available=False,
                use_reranker_when_available=False,
            )
        )
        pipeline = Text2SQLPipeline(
            PipelineConfig(db_path=Path(self.temp_dir.name) / "showroom.db")
        )
        service = MemoryAwareSqlAgentService(pipeline, memory)
        try:
            response = service.run(
                user_id="u1",
                session_id="s1",
                query="查询准时率低于0.9的供应商名称",
                enable_long_term=False,
            )
            messages = memory.store.list_messages(user_id="u1", session_id="s1")
            self.assertEqual([message.role for message in messages], [MessageRole.USER, MessageRole.ASSISTANT])
            self.assertTrue(response.pipeline_result.step_logs[0].execution_result["success"])
            self.assertEqual(memory.store.list_long_term_memories(user_id="u1"), [])

            saved = service.save_result_to_personal_knowledge_base(
                user_id="u1", result=response.pipeline_result
            )
            self.assertEqual(saved.kind, MemoryKind.STRUCTURED)
            self.assertTrue(saved.metadata["sql"])
            self.assertEqual(saved.metadata["database"], "erp_db")
            self.assertIn("on_time_rate < 0.9", saved.metadata["filters"]["sql_where"][0])

            follow_up = service.run(
                user_id="u1",
                session_id="s1",
                query="之前查询的供应商是多少",
                enable_long_term=True,
            )
            self.assertEqual(follow_up.memory_context.long_term_hits[0].memory.id, saved.id)
        finally:
            memory.close()


PURCHASE_SQL = (
    "SELECT po_no, job_no, supplier_id, po_amt, po_date, po_status "
    "FROM purchase_order WHERE po_status = 'received'"
)

# 与真实的 Schema 上下文同一量级（离线管线对一个 demo 问题给出约 7 千字符）。
# 它只服务于生成 SQL，不是对话里的事实，不该出现在摘要提示词里。
SCHEMA_CONTEXT = "数据库：erp_db\n表名：purchase_order\n" + "\n".join(
    f"- 字段名：po_field_{index}。仅供生成 SQL 使用的字段说明。" for index in range(200)
)
COT_OUTPUT = "步骤1：\n(\n  数据库: erp_db,\n  处理对象: purchase_order,\n  预期输出量级: 数千条\n)"

LEGACY_SUMMARY_PROMPT = """你是会话记忆压缩器。请把已有摘要与新增对话合并成一份事实性摘要。
必须保留：用户目标、已确认业务口径、筛选条件、关键 SQL/结果、分析结论、未完成任务。
不得补充原文没有的信息。直接输出摘要正文。

已有摘要：
{previous}

新增对话：
{records}
"""


class RecordingClient:
    """只记下提示词的假模型客户端：这些用例里没有任何真实的模型调用。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "（假摘要）"


class QueuedPipeline:
    """按顺序交出事先备好的结果，代替真实管线：不查库，也不调模型。"""

    def __init__(self, *results: PipelineResult) -> None:
        self.results = list(results)

    def run(self, query, keywords=None, conversation_context="", access=None, user_id=""):
        return self.results.pop(0)


def purchase_rows(count: int) -> list[dict]:
    """与 demo 库 purchase_order 同形状的行：6 列，每行约 137 个字符的 JSON。"""
    return [
        {
            "po_no": f"PO-24-{index:05d}",
            "job_no": f"J24{index % 40:03d}",
            "supplier_id": f"SUP-{index % 5 + 1:02d}",
            "po_amt": 1000.0 + index,
            "po_date": f"2024-{index % 12 + 1:02d}-15",
            "po_status": "received",
        }
        for index in range(count)
    ]


def executed(
    rows: list[dict],
    *,
    database: str = "erp_db",
    sql: str = PURCHASE_SQL,
    truncated: bool = False,
    row_limit: int = 1000,
) -> dict:
    """执行结果的形状直接取执行器的 to_dict()，那边的字段改了这里跟着变。"""
    return MCPExecutionResult(
        database=database,
        sql=sql,
        success=True,
        columns=list(rows[0]) if rows else [],
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        row_limit=row_limit,
        as_of="2026-10-05T16:00:00.000Z",
        as_of_source="query_time",
    ).to_dict()


def query_result(query: str, *executions: dict) -> PipelineResult:
    return PipelineResult(
        query=query,
        keywords=["采购单"],
        schema_context=SCHEMA_CONTEXT,
        cot_output=COT_OUTPUT,
        step_logs=[
            StepExecutionLog(
                database=execution["database"],
                cot_step=object(),
                local_schema="purchase_order",
                sql=execution["sql"],
                execution_request={"database": execution["database"], "sql": execution["sql"]},
                execution_result=execution,
                final_sql=execution["sql"],
            )
            for execution in executions
        ],
    )


def sql_result_message(payload: dict, content: str = "已完成数据库查询。") -> MemoryMessage:
    return MemoryMessage(
        id=1,
        session_id="s1",
        user_id="u1",
        role=MessageRole.ASSISTANT,
        content=content,
        message_type="sql_result",
        payload=payload,
    )


def instructions_in(prompt: str) -> str:
    """摘要提示词里写给模型的规则部分（「已有摘要」之前）。"""
    return prompt.split("已有摘要：", 1)[0]


def records_in(prompt: str) -> list[dict]:
    """摘要提示词里「新增对话」后面的那段 JSON。"""
    return json.loads(prompt.split("新增对话：\n", 1)[1])


def chars(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


class LargeResultSummaryTest(unittest.TestCase):
    """
    查库回答把整份 PipelineResult 存成消息的 payload。这条消息被挤出短期窗口时，
    模型摘要器读到的必须是有上限的摘录，而不是每一行结果。

    走的是真实的存储、窗口选择和摘要调度，只有管线和模型是假的。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.client = RecordingClient()

    def build_memory(self, **short_term) -> ConversationMemoryService:
        # 同步摘要只是为了让断言不必等后台线程；窗口大小用默认值
        short_term.setdefault("async_summary", False)
        memory = ConversationMemoryService(
            MemoryServiceConfig(
                db_path=Path(self.temp_dir.name) / "memory.db",
                embedding_dimensions=128,
                vector_backend="none",
                use_reranker_when_available=False,
                short_term=ShortTermMemoryConfig(**short_term),
            ),
            summarizer=ModelMemorySummarizer(self.client),
        )
        self.addCleanup(memory.close)
        return memory

    def ask_then_follow_up(
        self, memory: ConversationMemoryService, result: PipelineResult, session_id: str = "s1"
    ):
        """问一个查库问题，再接着说一句话；返回带着那条查库回答的摘要提示词和第二轮的上下文。"""
        already_recorded = len(self.client.prompts)
        service = MemoryAwareSqlAgentService(QueuedPipeline(result), memory)
        service.run(user_id="u1", session_id=session_id, query=result.query)
        context = memory.begin_user_turn(
            user_id="u1", session_id=session_id, query="解释一下刚才的结果"
        )
        prompts = [
            prompt
            for prompt in self.client.prompts[already_recorded:]
            if '"sql_result"' in prompt
        ]
        return prompts, context

    def test_a_result_at_the_row_cap_reaches_the_model_as_a_bounded_excerpt(self) -> None:
        memory = self.build_memory()
        rows = purchase_rows(1000)
        result = query_result("列出全部采购单", executed(rows, truncated=True))

        prompts, context = self.ask_then_follow_up(memory, result)

        # 按整份 payload 计 token，这条回答远超 4000 的窗口预算：用户说下一句话时它就出了窗口
        self.assertEqual([m.message_type for m in context.short_term.messages], ["text"])
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertLess(
            len(prompt),
            8000,
            f"一条 1000 行的查库回答让摘要提示词长到了 {len(prompt):,} 个字符",
        )

        (record,) = records_in(prompt)
        self.assertLessEqual(chars(record["payload"]), SummaryBudgetConfig().max_payload_chars)
        self.assertEqual(record["payload"]["query"], "列出全部采购单")
        (step,) = record["payload"]["steps"]
        self.assertEqual(step["database"], "erp_db")
        self.assertEqual(step["sql"], PURCHASE_SQL)
        self.assertIs(step["success"], True)
        self.assertEqual(step["row_count"], 1000)
        self.assertIs(step["truncated"], True)
        self.assertEqual(step["row_limit"], 1000)
        self.assertEqual(step["columns"], list(rows[0]))
        # 附上的是开头的若干行，原样；没附的行数写在明处
        self.assertGreater(len(step["rows"]), 0)
        self.assertEqual(step["rows"], rows[: len(step["rows"])])
        self.assertEqual(step["rows_omitted"], 1000 - len(step["rows"]))
        self.assertNotIn(rows[-1]["po_no"], prompt)
        # 生成 SQL 用的 Schema 上下文和规划文本不是对话里的事实
        self.assertNotIn("仅供生成 SQL 使用的字段说明", prompt)
        self.assertNotIn("预期输出量级", prompt)

    def test_the_prompt_says_that_rows_were_left_out(self) -> None:
        memory = self.build_memory()
        result = query_result("列出全部采购单", executed(purchase_rows(1000), truncated=True))

        (prompt,), _ = self.ask_then_follow_up(memory, result)

        instructions = instructions_in(prompt)
        self.assertIn("rows_omitted", instructions)
        self.assertIn("不要把样例当成完整结果", instructions)
        # 结果在执行时就被行数上限截断过：row_count 不是符合条件的总数
        self.assertIn("row_limit", instructions)
        self.assertIn("符合条件的记录多于 row_count 行", instructions)

    def test_the_prompt_does_not_grow_with_the_row_cap(self) -> None:
        lengths = {}
        for cap in (1000, 10000):
            memory = self.build_memory()
            result = query_result(
                "列出全部采购单", executed(purchase_rows(cap), truncated=True, row_limit=cap)
            )
            (prompt,), _ = self.ask_then_follow_up(memory, result, session_id=f"cap-{cap}")
            lengths[cap] = len(prompt)

        # 差出来的只有 row_count / row_limit / rows_omitted 这几个数的位数
        self.assertLess(abs(lengths[10000] - lengths[1000]), 20, lengths)

    def test_the_background_summary_job_reads_the_same_excerpt_and_completes(self) -> None:
        memory = self.build_memory(async_summary=True)
        result = query_result("列出全部采购单", executed(purchase_rows(1000), truncated=True))
        service = MemoryAwareSqlAgentService(QueuedPipeline(result), memory)

        service.run(user_id="u1", session_id="s1", query=result.query)
        memory.begin_user_turn(user_id="u1", session_id="s1", query="解释一下刚才的结果")

        # 后台任务出错只会留下一条日志、摘要从此停更；这里等到它确实走完并落了库。
        # 两批摘要是接力排的，中间有一瞬间查不到任务，所以按结果等，而不是只等一次任务。
        deadline = time.monotonic() + 5
        summary = None
        while time.monotonic() < deadline:
            memory.short_term.wait_for_summary(user_id="u1", session_id="s1", timeout=5)
            summary = memory.get_context(user_id="u1", session_id="s1").short_term.summary
            if summary is not None and summary.through_message_id == 2:
                break
            time.sleep(0.01)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.through_message_id, 2)
        self.assertEqual(summary.content, "（假摘要）")
        (prompt,) = [prompt for prompt in self.client.prompts if '"sql_result"' in prompt]
        self.assertLess(len(prompt), 8000)

    def test_the_stored_message_keeps_every_row(self) -> None:
        memory = self.build_memory()
        result = query_result("列出全部采购单", executed(purchase_rows(1000), truncated=True))

        self.ask_then_follow_up(memory, result)

        stored = [
            message
            for message in memory.store.list_messages(user_id="u1", session_id="s1")
            if message.message_type == "sql_result"
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].payload, json.loads(json.dumps(result.to_dict(), default=str)))
        self.assertEqual(len(stored[0].payload["step_logs"][0]["execution_result"]["rows"]), 1000)

    def test_a_follow_up_inside_the_window_is_still_answered_from_all_rows(self) -> None:
        memory = self.build_memory()
        amounts = [{"po_amt": 1000.0 + index} for index in range(40)]
        result = query_result("各采购单的金额", executed(amounts))

        prompts, context = self.ask_then_follow_up(memory, result)

        # 这条回答还在窗口里，没有送去摘要；规则版追问读的是存下来的完整结果
        self.assertEqual(prompts, [])
        answer = RuleBasedDataQAClient(definitions={}).answer("分析一下最高和最低", context)
        self.assertIn("返回 40 条记录", answer)
        self.assertIn("最大值为 1039.0", answer)

    def test_a_small_result_is_passed_whole_without_an_omission_note(self) -> None:
        memory = self.build_memory(max_window_messages=1)
        rows = purchase_rows(3)
        result = query_result("列出已到货的采购单", executed(rows))

        (prompt,), _ = self.ask_then_follow_up(memory, result)

        (record,) = records_in(prompt)
        (step,) = record["payload"]["steps"]
        self.assertEqual(step["rows"], rows)
        self.assertEqual(step["rows_omitted"], 0)
        self.assertEqual(step["row_count"], 3)
        self.assertIs(step["truncated"], False)
        # 没有省略任何行，就不该提醒模型「这只是样例」
        self.assertEqual(
            instructions_in(prompt),
            instructions_in(LEGACY_SUMMARY_PROMPT),
        )
        self.assertNotIn("仅供生成 SQL 使用的字段说明", prompt)


class SummaryInputBudgetTest(unittest.TestCase):
    """直接对着 ModelMemorySummarizer：每条消息进提示词的内容都有硬上限。"""

    def summarize(self, *messages: MemoryMessage, budget: SummaryBudgetConfig | None = None) -> str:
        client = RecordingClient()
        ModelMemorySummarizer(client, budget).summarize_conversation("", list(messages))
        (prompt,) = client.prompts
        return prompt

    def test_messages_without_payload_produce_the_same_prompt_as_before(self) -> None:
        messages = [
            MemoryMessage(id=1, session_id="s1", user_id="u1", role=MessageRole.USER,
                          content="准时率低于 0.9 的供应商有哪些？"),
            MemoryMessage(id=2, session_id="s1", user_id="u1", role=MessageRole.ASSISTANT,
                          content="有三家：东顺五金、…", message_type="data_qa"),
        ]
        client = RecordingClient()

        ModelMemorySummarizer(client).summarize_conversation("用户在看供应商准时率", messages)

        expected_records = [
            {"role": "user", "content": "准时率低于 0.9 的供应商有哪些？",
             "message_type": "text", "payload": {}},
            {"role": "assistant", "content": "有三家：东顺五金、…",
             "message_type": "data_qa", "payload": {}},
        ]
        self.assertEqual(
            client.prompts,
            [
                LEGACY_SUMMARY_PROMPT.format(
                    previous="用户在看供应商准时率",
                    records=json.dumps(expected_records, ensure_ascii=False),
                )
            ],
        )

    def test_no_payload_shape_gets_past_the_budget(self) -> None:
        wide_row = {f"column_{index:03d}": index for index in range(400)}
        payloads = {
            "十二步，每步五千字符的 SQL": {
                "query": "q",
                "step_logs": [
                    {
                        "database": "erp_db",
                        "final_sql": "SELECT " + "x, " * 1700,
                        "execution_result": executed(purchase_rows(50)),
                    }
                    for _ in range(12)
                ],
            },
            "四百列的宽表": {
                "step_logs": [
                    {
                        "database": "erp_db",
                        "sql": "SELECT * FROM wide",
                        "execution_result": {
                            "success": True,
                            "columns": list(wide_row),
                            "rows": [wide_row] * 50,
                            "row_count": 50,
                        },
                    }
                ]
            },
            "单元格里有十万字符": {
                "step_logs": [
                    {
                        "execution_result": {
                            "success": True,
                            "columns": ["note"],
                            "rows": [{"note": "备注" * 50_000}] * 3,
                            "row_count": 3,
                        }
                    }
                ]
            },
            "行不是字典": {
                "step_logs": [
                    {
                        "execution_result": {
                            "success": True,
                            "rows": [[1, "a" * 5000], 7, None, "x" * 9000],
                        }
                    }
                ]
            },
            "步骤里混着别的东西": {
                "step_logs": [
                    None,
                    "oops",
                    3,
                    {"execution_result": "not a dict"},
                    {
                        "database": {"nested": "x" * 4000},
                        "execution_result": {"rows": "not a list"},
                    },
                ]
            },
            "失败的步骤带着五万字符的报错": {
                "step_logs": [
                    {
                        "database": "erp_db",
                        "sql": PURCHASE_SQL,
                        "execution_result": {
                            "success": False,
                            "error": "no such column " * 3400,
                            "rows": [],
                        },
                    }
                ]
            },
            "六万字符的问题": {"query": "为什么" * 20_000, "step_logs": []},
            "列名本身很长": {
                "step_logs": [
                    {
                        "execution_result": {
                            "success": True,
                            "columns": ["c" * 3000, "d" * 3000],
                            "rows": [{"c" * 3000: 1, "d" * 3000: 2}],
                        }
                    }
                ]
            },
            "不是查库结果的大 payload": {"analysis": "结论" * 50_000, "figures": list(range(5000))},
            "满是引号和换行的大 payload": {"text": '"\n\\' * 30_000},
        }
        budgets = {
            "默认预算": SummaryBudgetConfig(),
            "很紧的预算": SummaryBudgetConfig(
                max_content_chars=60, max_payload_chars=200, max_sql_chars=20, max_cell_chars=5
            ),
        }

        for budget_name, budget in budgets.items():
            for name, payload in payloads.items():
                with self.subTest(budget=budget_name, payload=name):
                    before = copy.deepcopy(payload)
                    prompt = self.summarize(
                        sql_result_message(payload, content="查询结果：" + "很长的回答" * 4000),
                        budget=budget,
                    )
                    (record,) = records_in(prompt)
                    self.assertLessEqual(chars(record["payload"]), budget.max_payload_chars)
                    self.assertLessEqual(len(record["content"]), budget.max_content_chars)
                    # 摘要器只是读，传进来的消息原样不动
                    self.assertEqual(payload, before)

    def test_long_text_is_cut_in_the_middle_with_a_visible_marker(self) -> None:
        content = "开头的结论。" + "中间的明细，" * 20_000 + "结尾的结论。"

        prompt = self.summarize(
            MemoryMessage(id=1, session_id="s1", user_id="u1", role=MessageRole.ASSISTANT,
                          content=content, message_type="data_qa")
        )

        (record,) = records_in(prompt)
        kept = record["content"]
        self.assertLessEqual(len(kept), SummaryBudgetConfig().max_content_chars)
        self.assertTrue(kept.startswith("开头的结论。"))
        self.assertTrue(kept.endswith("结尾的结论。"))
        # 标记里写的省略字数，就是原文里真正没留下的字数
        head, stated, tail = re.split(r"……（此处省略 (\d+) 字符）……", kept)
        self.assertEqual(int(stated), len(content) - len(head) - len(tail))

    def test_the_final_step_gets_its_rows_before_earlier_steps(self) -> None:
        first = executed(purchase_rows(800), database="pm_db", sql="SELECT job_no FROM project")
        final_rows = purchase_rows(12)
        final = executed(final_rows, sql="SELECT po_no FROM purchase_order WHERE job_no IN (...)")
        payload = query_result("超预算项目的采购单", first, final).to_dict()

        (record,) = records_in(self.summarize(sql_result_message(payload)))

        earlier, last = record["payload"]["steps"]
        self.assertEqual((earlier["database"], last["database"]), ("pm_db", "erp_db"))
        self.assertEqual(earlier["sql"], "SELECT job_no FROM project")
        self.assertEqual(earlier["row_count"], 800)
        # 最后一步才是用户看到的答案：先保它的行，剩下的预算才轮到前面的步骤
        self.assertEqual(last["rows"], final_rows)
        self.assertEqual(last["rows_omitted"], 0)
        self.assertEqual(earlier["rows_omitted"], 800 - len(earlier["rows"]))
        self.assertGreater(earlier["rows_omitted"], 0)

    def test_only_the_last_steps_of_a_very_long_plan_are_kept(self) -> None:
        executions = [
            executed(purchase_rows(1), sql=f"SELECT {index} FROM purchase_order")
            for index in range(8)
        ]
        payload = query_result("八步的查询", *executions).to_dict()

        (record,) = records_in(self.summarize(sql_result_message(payload)))

        self.assertEqual(record["payload"]["steps_omitted"], 3)
        self.assertEqual(
            [step["sql"] for step in record["payload"]["steps"]],
            [f"SELECT {index} FROM purchase_order" for index in range(3, 8)],
        )

    def test_other_payloads_pass_through_when_small_and_are_cut_when_large(self) -> None:
        small = {"on_time_rate": 0.87}
        large = {"analysis": "结论" * 50_000}

        small_record, large_record = records_in(
            self.summarize(
                sql_result_message(small, content="准时率是 87%"),
                sql_result_message(large, content="分析见附件"),
            )
        )

        self.assertEqual(small_record["payload"], small)
        self.assertEqual(
            large_record["payload"]["note"], "payload 的 JSON 共 100016 字符，此处只保留开头"
        )
        self.assertTrue(large_record["payload"]["payload_excerpt"].startswith('{"analysis": "结论'))

    def test_every_field_of_a_stored_result_is_either_read_or_knowingly_skipped(self) -> None:
        """
        摘要器按名字从存下来的查库结果里取字段。to_dict() 多出一个字段时这条用例会失败：
        请把新字段归到下面「读」或「不读」的一边，并相应地改 summarizer._result_excerpt——
        否则它会悄悄地到不了摘要。
        """
        payload = query_result("列出全部采购单", executed(purchase_rows(2))).to_dict()
        (step_log,) = payload["step_logs"]

        self.assertEqual(
            set(payload),
            {"query", "degraded", "degrade_reason", "step_logs"}  # 读
            | {"keywords", "schema_context", "cot_output"},  # 不读
        )
        self.assertEqual(
            set(step_log),
            {"database", "sql", "final_sql", "execution_result"}  # 读
            | {"execution_request", "repair_attempts"},  # 不读
        )
        self.assertEqual(
            set(step_log["execution_result"]),
            {
                "database",
                "sql",
                "success",
                "error",
                "columns",
                "rows",
                "row_count",
                "truncated",
                "row_limit",
            }  # 读
            | {"as_of", "as_of_source"},  # 不读
        )

    def test_a_failed_step_keeps_its_sql_and_error(self) -> None:
        failed = MCPExecutionResult(
            database="erp_db",
            sql="SELECT po_amount FROM purchase_order",
            success=False,
            error="no such column: po_amount",
        ).to_dict()
        payload = query_result("采购金额合计", failed).to_dict()

        prompt = self.summarize(sql_result_message(payload))

        (step,) = records_in(prompt)[0]["payload"]["steps"]
        self.assertIs(step["success"], False)
        self.assertEqual(step["error"], "no such column: po_amount")
        self.assertEqual(step["sql"], "SELECT po_amount FROM purchase_order")
        self.assertEqual((step["row_count"], step["rows"], step["rows_omitted"]), (0, [], 0))
        self.assertEqual(instructions_in(prompt), instructions_in(LEGACY_SUMMARY_PROMPT))

    def test_the_sql_that_actually_ran_is_the_one_kept(self) -> None:
        payload = query_result("采购金额合计", executed(purchase_rows(2))).to_dict()
        payload["step_logs"][0]["sql"] = "SELECT po_amount FROM purchase_order"
        payload["step_logs"][0]["final_sql"] = "SELECT po_amt FROM purchase_order"
        payload["degraded"] = True
        payload["degrade_reason"] = "model_unavailable_replayed_sql"

        (record,) = records_in(self.summarize(sql_result_message(payload)))

        self.assertEqual(record["payload"]["steps"][0]["sql"], "SELECT po_amt FROM purchase_order")
        self.assertIs(record["payload"]["degraded"], True)
        self.assertEqual(record["payload"]["degrade_reason"], "model_unavailable_replayed_sql")

    def test_a_long_cell_is_shortened_instead_of_dropping_the_row(self) -> None:
        rows = [{"po_no": "PO-24-00001", "remark": "备注" * 3000}]
        payload = query_result("带备注的采购单", executed(rows)).to_dict()

        (record,) = records_in(self.summarize(sql_result_message(payload)))

        (step,) = record["payload"]["steps"]
        (row,) = step["rows"]
        self.assertEqual(row["po_no"], "PO-24-00001")
        self.assertTrue(row["remark"].startswith("备注备注"))
        self.assertTrue(row["remark"].endswith("…（共 6000 字符）"))
        self.assertEqual(step["rows_omitted"], 0)

    def test_budgets_too_small_to_hold_the_markers_are_rejected(self) -> None:
        for field_name in (
            "max_content_chars",
            "max_payload_chars",
            "max_sql_chars",
            "max_cell_chars",
        ):
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                SummaryBudgetConfig(**{field_name: 0})


if __name__ == "__main__":
    unittest.main()
