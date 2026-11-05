from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from ai_sql_agent.memory import (
    ConversationMemoryService,
    MemoryServiceConfig,
    ShortTermMemoryConfig,
)
from ai_sql_agent.pipeline import (
    Text2SQLPipeline,
    DynamicSqlAgentService,
    PipelineConfig,
)


def print_section(title: str) -> None:
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


def preview(text: str, max_chars: int = 900) -> str:
    compact = text.strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars] + "\n……（Demo 展示已截断，数据库中保留完整内容）"


def print_pipeline_result(result) -> None:
    print(f"用户问题：{result.query}")
    for index, log in enumerate(result.step_logs, start=1):
        print(f"\n步骤 {index} SQL：")
        print(log.sql)
        print("执行结果：")
        print(json.dumps(log.execution_result, ensure_ascii=False, indent=2))


def print_short_term_context(context) -> None:
    print(f"摘要任务执行中：{context.summary_pending}")
    print("历史摘要：")
    print(preview(context.summary.content) if context.summary else "（尚无摘要）")
    print("近期滑动窗口：")
    if not context.messages:
        print("（暂无消息）")
        return
    for message in context.messages:
        print(
            f"- [{message.role.value}/{message.message_type}] "
            f"{preview(message.content, max_chars=500)}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI SQL Agent 长短期记忆端到端 Demo")
    parser.add_argument("--user-id", default="", help="演示用户 ID；不传则自动生成")
    parser.add_argument("--session-id", default="", help="演示会话 ID；不传则自动生成")
    parser.add_argument(
        "--memory-db",
        default=str(Path("runtime_data") / "memory_demo.db"),
        help="记忆 SQLite 文件路径",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_id = uuid.uuid4().hex[:8]
    user_id = args.user_id or f"memory-demo-user-{run_id}"
    session_id = args.session_id or f"memory-demo-session-{run_id}"

    memory = ConversationMemoryService(
        MemoryServiceConfig(
            db_path=Path(args.memory_db),
            # 只保留最近两条消息，方便在第二轮直观看到异步摘要效果。
            short_term=ShortTermMemoryConfig(
                max_window_messages=2,
                max_window_tokens=2000,
                async_summary=True,
            ),
        )
    )
    pipeline = Text2SQLPipeline(
        PipelineConfig(
            database_name="erp_db",
            db_path=Path("runtime_data") / "erp_demo.db",
            sample_size=5,
        )
    )
    service = DynamicSqlAgentService(pipeline=pipeline, memory=memory)

    try:
        print_section("0. Demo 身份")
        print(f"user_id：{user_id}")
        print(f"session_id：{session_id}")

        print_section("1. 第一轮：执行 Text2SQL，并自动写入短期记忆")
        first = service.run(
            user_id=user_id,
            session_id=session_id,
            query="查询准时率低于0.9的供应商名称",
            enable_long_term=False,
        )
        print(f"路由结果：{first.decision.route.value}")
        print(f"路由原因：{first.decision.reason}")
        print_pipeline_result(first.pipeline_result)

        first_context = memory.get_context(
            user_id=user_id,
            session_id=session_id,
        )
        print_section("2. 第一轮后的短期记忆")
        print_short_term_context(first_context.short_term)

        print_section("3. 模拟用户点击“保存到个人知识库”")
        saved = service.save_result_to_personal_knowledge_base(
            user_id=user_id,
            result=first.pipeline_result,
        )
        print(f"长期记忆 ID：{saved.id}")
        print(f"记忆类型：{saved.kind.value}")
        print(f"检索摘要：{preview(saved.summary)}")
        print("结构化元信息：")
        print(json.dumps(saved.metadata, ensure_ascii=False, indent=2, default=str))

        print_section("4. 第二轮：读取短期上下文，并开启长期记忆召回")
        second = service.run(
            user_id=user_id,
            session_id=session_id,
            query="之前查询到的供应商有哪些？",
            enable_long_term=True,
            long_term_top_k=3,
        )

        print(f"路由结果：{second.decision.route.value}")
        print(f"路由原因：{second.decision.reason}")
        print(f"是否查询数据库：{second.queried_database}")

        print("本轮送入查询链路的记忆上下文：")
        context_text = second.memory_context.to_prompt_context()
        print(preview(context_text, max_chars=1600) if context_text else "（无记忆上下文）")
        print("\n长期记忆召回结果：")
        if second.memory_context.long_term_hits:
            for index, hit in enumerate(second.memory_context.long_term_hits, start=1):
                print(
                    f"- {index}. score={hit.score:.4f}, "
                    f"memory_id={hit.memory.id}\n  {preview(hit.memory.summary)}"
                )
        else:
            print("（未召回长期记忆）")

        print("\n第二轮对话问答结果：")
        print(second.answer)

        # 等待后台摘要完成只用于 Demo 展示；正常在线请求不需要等待。
        memory.short_term.wait_for_summary(
            user_id=user_id,
            session_id=session_id,
            timeout=30,
        )
        final_context = memory.get_context(
            user_id=user_id,
            session_id=session_id,
            query="之前查询到的供应商有哪些？",
            enable_long_term=True,
        )
        print_section("5. 第二轮完成后的最终记忆状态")
        print_short_term_context(final_context.short_term)
        print(f"长期记忆召回数量：{len(final_context.long_term_hits)}")

        print_section("6. Demo 完成")
        print(f"记忆数据库：{Path(args.memory_db).resolve()}")
        print("默认每次运行自动生成用户和会话，避免旧数据干扰。")
        print("如需复用历史记忆，请再次运行时显式传入上面显示的 --user-id。")
    finally:
        memory.close(wait=True)


if __name__ == "__main__":
    main()
