from __future__ import annotations

import unittest

from ai_sql_agent.execution import MCPExecutionRequest
from ai_sql_agent.auth import authorization_scope
from ai_sql_agent.execution.sqlite_executor import SQLiteMCPExecutor
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.generation.objects import LocalSchema, TableColumn, TableSchema
from ai_sql_agent.repair import ExecutionLogParser, SqlClauseLocalizer, SqlRepairService

from ai_sql_agent.pipeline.demo_data import create_erp_demo_database


def _build_erp_local_schema() -> LocalSchema:
    return LocalSchema(
        tables={
            "supplier": TableSchema(
                table_name="supplier",
                columns=[
                    TableColumn(column_name="supplier_id", data_type="text"),
                    TableColumn(column_name="supplier_name", data_type="text"),
                    TableColumn(column_name="on_time_rate", data_type="real"),
                ],
            ),
            "purchase_order": TableSchema(
                table_name="purchase_order",
                columns=[
                    TableColumn(column_name="po_no", data_type="text"),
                    TableColumn(column_name="po_amt", data_type="real"),
                ],
            ),
        },
    )


class LogParserTestCase(unittest.TestCase):
    def test_parses_sqlite_error_family(self):
        parser = ExecutionLogParser()

        error = parser.parse("no such table: supplier_info")
        self.assertEqual(error.error_type, "no_such_table")
        self.assertEqual(error.detail, "supplier_info")

        error = parser.parse("no such column: supplier_namel")
        self.assertEqual(error.error_type, "no_such_column")

        error = parser.parse('near "SELETC": syntax error')
        self.assertEqual(error.error_type, "syntax_error")
        self.assertEqual(error.detail, "SELETC")

        error = parser.parse("查询执行超时（超过 10.0 秒），已中止。")
        self.assertEqual(error.error_type, "timeout")

        error = parser.parse("something completely different")
        self.assertEqual(error.error_type, "unknown")


class ClauseLocalizerTestCase(unittest.TestCase):
    def setUp(self):
        self.localizer = SqlClauseLocalizer()
        self.parser = ExecutionLogParser()

    def test_localizes_wrong_column_to_where_clause(self):
        sql = (
            "SELECT supplier_name FROM supplier "
            "WHERE order_noo = 1001"
        )
        error = self.parser.parse("no such column: order_noo")
        clause = self.localizer.localize(sql, error)
        self.assertIsNotNone(clause)
        self.assertEqual(clause.name, "WHERE")

    def test_localizes_wrong_table_to_from_clause(self):
        sql = "SELECT supplier_name FROM supplier_info"
        error = self.parser.parse("no such table: supplier_info")
        clause = self.localizer.localize(sql, error)
        self.assertIsNotNone(clause)
        self.assertEqual(clause.name, "FROM")

    def test_timeout_returns_none(self):
        error = self.parser.parse("查询执行超时（超过 10.0 秒），已中止。")
        clause = self.localizer.localize("SELECT 1", error)
        self.assertIsNone(clause)


class RepairServiceTestCase(unittest.TestCase):
    """在真实 SQLite 执行器上验证修复回路。"""

    @classmethod
    def setUpClass(cls):
        cls.db_path = create_erp_demo_database("runtime_data/repair_test.db")
        cls.executor = SQLiteMCPExecutor("erp_db", cls.db_path)

    def _execute(self, database: str, sql: str):
        # 执行器入口有强制授权关卡，不绑作用域一律拒绝。这组用例验的是修复回路
        # 本身，权限维度用全放开策略排除掉（越权不该进修复回路，那条在
        # tests/test_authorization_boundary.py 里单独验）。
        with authorization_scope(ALLOW_ALL):
            return self.executor.execute(
                MCPExecutionRequest(database=database, sql=sql)
            )

    def _run_repair(self, sql: str):
        first = self._execute("erp_db", sql)
        self.assertFalse(first.success)

        service = SqlRepairService(coder_client=None, max_attempts=2)
        return service.repair(
            database="erp_db",
            sql=sql,
            error_message=first.error or "",
            local_schema=_build_erp_local_schema(),
            execute=self._execute,
        )

    def test_repairs_wrong_table_name(self):
        outcome = self._run_repair(
            "SELECT supplier_name FROM supplier_infos WHERE supplier_id = 'SUP-01'"
        )
        self.assertTrue(outcome.repaired)
        self.assertIn("FROM supplier", outcome.final_sql)
        self.assertEqual(outcome.final_result.row_count, 1)
        self.assertEqual(outcome.attempts[0].error_type, "no_such_table")
        self.assertEqual(outcome.attempts[0].strategy, "heuristic")

    def test_repairs_wrong_column_name(self):
        outcome = self._run_repair(
            "SELECT supplier_namel FROM supplier WHERE supplier_id = 'SUP-01'"
        )
        self.assertTrue(outcome.repaired)
        self.assertIn("supplier_name", outcome.final_sql)
        self.assertTrue(outcome.final_result.success)

    def test_gives_up_on_unrepairable_sql(self):
        outcome = self._run_repair("SELECT * FROM totally_unrelated_name_xyz")
        # 近名匹配找不到候选时应放弃而不是死循环
        self.assertFalse(outcome.repaired)
        self.assertLessEqual(len(outcome.attempts), 2)


if __name__ == "__main__":
    unittest.main()


class CotParserRobustnessTest(unittest.TestCase):
    """CoT 解析必须容忍真实模型的分隔符写法差异。

    早期实现要求字段之间是半角逗号，模型经常写全角「，」、以「；」收尾、
    或末字段不带逗号——任意一处不匹配整步就丢失，表现为「未生成执行步骤」。
    实测这一条吃掉了约三成的评测样本，因此加回归防护。
    """

    def setUp(self):
        from ai_sql_agent.generation.cot_parser import CotStepParser

        self.parser = CotStepParser()

    def test_parses_field_ending_with_fullwidth_semicolon(self):
        text = (
            "步骤1：\n(\n  数据库: erp_db,\n"
            "  处理对象: 表sales_order，涉及字段order_status、customer_name，主键order_no；\n"
            "  操作指令: 先确定筛选字段；再筛选order_status = 'in_production'的记录,\n"
            "  输出目标: customer_name\n)"
        )
        steps = self.parser.parse_many(text)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].database, "erp_db")
        self.assertEqual(steps[0].output_target, "customer_name")

    def test_parses_legacy_halfwidth_comma_format(self):
        text = (
            "步骤1：\n(\n  数据库: erp_db,\n  处理对象: supplier.on_time_rate,\n"
            "  操作指令: 筛选,\n  输出目标: supplier_name\n)"
        )
        steps = self.parser.parse_many(text)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].output_target, "supplier_name")

    def test_multi_step_does_not_swallow_block_boundary(self):
        text = (
            "步骤1：\n(\n数据库: pm_db,\n处理对象: a,\n操作指令: b,\n输出目标: project_code\n)\n"
            "步骤2：\n(\n数据库: erp_db,\n处理对象: c,\n操作指令: d,\n"
            "输出目标: po_amt,\n预期输出量级: 数条\n)"
        )
        steps = self.parser.parse_many(text)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].output_target, "project_code")
        self.assertEqual(steps[1].expected_cardinality, "数条")

    def test_blocks_without_step_markers(self):
        text = (
            "(\n数据库: pm_db,\n处理对象: a,\n操作指令: b,\n输出目标: x\n)\n"
            "(\n数据库: erp_db,\n处理对象: c,\n操作指令: d,\n输出目标: y\n)"
        )
        self.assertEqual(len(self.parser.parse_many(text)), 2)

    def test_incomplete_step_is_dropped(self):
        self.assertEqual(self.parser.parse_many("数据库: erp_db,\n处理对象: x"), [])


class SqlPromptConstraintTest(unittest.TestCase):
    """SQL 生成 Prompt 必须禁止库名前缀。

    模型看到 Schema 里的「数据库：pm_db」后容易写成 `pm_db.project`，
    但执行时已连到目标库，会直接报 no such table。
    """

    def test_prompt_forbids_database_prefix(self):
        from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest
        from ai_sql_agent.generation.prompt_builder import SqlPromptBuilder

        prompt = SqlPromptBuilder().build(
            SqlGenerationRequest(
                cot_step=CotStep(
                    database="pm_db",
                    processing_objects="project.actual_cost",
                    operation_instruction="筛选超预算项目",
                    output_target="project.project_name",
                ),
                local_schema=LocalSchema(),
                sql_dialect="sqlite",
            )
        )
        self.assertIn("数据库名前缀", prompt)


class PlannerParserUnificationTest(unittest.TestCase):
    """规划器与 SQL 生成必须共用同一个 CoT 解析器。

    项目里一度存在两份解析同一格式的正则，修了下游那份、上游那份还在漏，
    真实模型评测中表现为 38 条「未生成执行步骤」。这里锁住「只有一份实现」。
    """

    def setUp(self):
        from ai_sql_agent.planning.cot_planner import CotPlanner

        self.planner = CotPlanner()

    def test_planner_handles_fullwidth_parens_and_commas(self):
        """处理对象里写 `material表（字段：a, b, c）` 时不能整步丢失。"""
        text = (
            "步骤1：\n(\n  数据库: wms_db,\n"
            "  处理对象: material表（字段：material_code, material_name, category），\n"
            "  操作指令: 筛选category为“门板”的记录,\n"
            "  输出目标: material_name\n)"
        )
        steps = self.planner.parse_steps(text)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].database, "wms_db")
        self.assertEqual(steps[0].output_target, "material_name")

    def test_planner_handles_missing_step_marker(self):
        text = "(\n数据库: pm_db,\n处理对象: a,\n操作指令: b,\n输出目标: x\n)"
        self.assertEqual(len(self.planner.parse_steps(text)), 1)

    def test_planner_numbers_steps_sequentially(self):
        text = (
            "步骤1：\n(\n数据库: pm_db,\n处理对象: a,\n操作指令: b,\n输出目标: project_code\n)\n"
            "步骤2：\n(\n数据库: erp_db,\n处理对象: c,\n操作指令: d,\n"
            "输出目标: po_amt,\n预期输出量级: 数条\n)"
        )
        steps = self.planner.parse_steps(text)
        self.assertEqual([s.step_no for s in steps], [1, 2])
        self.assertEqual(steps[1].expected_cardinality, "数条")

    def test_planner_does_not_define_its_own_regex(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "src" / "ai_sql_agent" / "planning" / "cot_planner.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "处理对象:\\s*", source,
            "规划器不应再自己写一份 CoT 正则，应复用 CotStepParser",
        )
