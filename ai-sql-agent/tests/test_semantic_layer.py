"""语义层：元数据从代码搬到配置之后，必须保证两件事。

1. 搬家没搬丢——内置 Demo 元数据导出成 YAML 再读回来，内容逐字节等价；
2. 元数据错了要在**接上去之前**被发现——写错表名、引用不存在的字段、
   漏写真实库里的表，这三类问题在运行时的现象都是「模型答不准」，
   靠人从答案倒推非常贵。
"""

import json
import tempfile
import unittest
from pathlib import Path

from ai_sql_agent.semantic import (
    DatabaseSemantics,
    SemanticLayer,
    demo_semantic_layer,
    load_semantic_dir,
    validate_semantic_layer,
    write_semantic_dir,
)


class DemoLayerTest(unittest.TestCase):
    def setUp(self):
        self.layer = demo_semantic_layer()

    def test_demo_layer_covers_four_databases(self):
        self.assertEqual(
            set(self.layer.database_names()),
            {"erp_db", "pm_db", "mes_db", "wms_db"},
        )
        self.assertTrue(self.layer.is_demo)

    def test_business_metas_match_legacy_demo_functions(self):
        """搬家不能改内容：语义层给出的元数据必须与原来的函数逐字相同。"""
        from ai_sql_agent.pipeline import demo_data

        self.assertEqual(
            self.layer.business_metas()["erp_db"], demo_data.get_erp_business_meta()
        )
        self.assertEqual(
            self.layer.business_metas()["wms_db"], demo_data.get_wms_business_meta()
        )
        self.assertEqual(
            self.layer.cross_database_links, demo_data.get_cross_database_links()
        )
        self.assertEqual(
            self.layer.metric_disambiguation, demo_data.get_metric_disambiguation()
        )

    def test_demo_metadata_passes_its_own_validator(self):
        """内置元数据必须自洽，否则校验器给出的标准就没有说服力。"""
        report = validate_semantic_layer(self.layer, live=True)
        self.assertTrue(report.ok, report.render())
        self.assertEqual(report.errors, [])

    def test_demo_metadata_matches_real_demo_databases(self):
        """跑到这里说明四个 Demo 库真的连上了，不是跳过了实库比对。"""
        report = validate_semantic_layer(self.layer, live=True)
        self.assertEqual(len(report.live_checked), 4, report.render())


class RoundTripTest(unittest.TestCase):
    """导出成文件再读回来，内容必须等价——否则「导出当模板」这条路就是错的。"""

    def _round_trip(self, fmt: str) -> SemanticLayer:
        demo = demo_semantic_layer()
        with tempfile.TemporaryDirectory() as temp:
            write_semantic_dir(demo, temp, fmt=fmt)
            return load_semantic_dir(temp)

    def test_yaml_round_trip_preserves_content(self):
        demo = demo_semantic_layer()
        disk = self._round_trip("yaml")
        self.assertEqual(demo.business_metas(), disk.business_metas())
        self.assertEqual(demo.cross_database_links, disk.cross_database_links)
        self.assertEqual(demo.metric_disambiguation, disk.metric_disambiguation)
        self.assertEqual(demo.metric_definitions, disk.metric_definitions)

    def test_json_round_trip_preserves_content(self):
        demo = demo_semantic_layer()
        disk = self._round_trip("json")
        self.assertEqual(demo.business_metas(), disk.business_metas())

    def test_catalog_content_survives_round_trip(self):
        """库级检索靠 catalog 的描述与别名，顺序可以变，内容不能丢。"""
        demo = demo_semantic_layer()
        disk = self._round_trip("yaml")
        by_name = lambda layer: {item["name"]: item for item in layer.catalog()}
        self.assertEqual(by_name(demo), by_name(disk))

    def test_export_refuses_to_clobber_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            write_semantic_dir(demo_semantic_layer(), temp)
            with self.assertRaises(FileExistsError):
                write_semantic_dir(demo_semantic_layer(), temp)
            # 显式要求覆盖时才允许
            write_semantic_dir(demo_semantic_layer(), temp, overwrite=True)


class LoaderTest(unittest.TestCase):
    def _write(self, directory: Path, name: str, payload: dict) -> None:
        (directory / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_loads_json_and_infers_name_from_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._write(
                directory,
                "sales.json",
                {
                    "description": "销售库",
                    "connection": {"dialect": "sqlite", "db_path": "x.db"},
                    "tables": {"t": {"columns": {"c": {"description": "字段"}}}},
                },
            )
            layer = load_semantic_dir(directory)
            self.assertEqual(layer.database_names(), ["sales"])
            self.assertFalse(layer.is_demo)

    def test_missing_directory_is_an_explicit_error(self):
        with self.assertRaises(FileNotFoundError):
            load_semantic_dir("/nope/does/not/exist")

    def test_empty_directory_explains_how_to_start(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError) as ctx:
                load_semantic_dir(temp)
            self.assertIn("export", str(ctx.exception))

    def test_duplicate_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            body = {"name": "dup", "tables": {"t": {"columns": {"c": {}}}}}
            self._write(directory, "a.json", body)
            self._write(directory, "b.json", body)
            with self.assertRaises(ValueError):
                load_semantic_dir(directory)

    def test_shared_files_are_merged_not_treated_as_databases(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self._write(
                directory,
                "one.json",
                {"tables": {"t": {"columns": {"c": {"description": "d"}}}}},
            )
            self._write(
                directory,
                "_shared.json",
                {"metric_definitions": {"良率": "合格数 / 送检数"}},
            )
            layer = load_semantic_dir(directory)
            self.assertEqual(layer.database_names(), ["one"])
            self.assertEqual(layer.metric_definitions["良率"], "合格数 / 送检数")

    def test_dsn_can_come_from_environment(self):
        """连接串带密码，不能进版本库；文件里只放环境变量名。"""
        import os

        os.environ["TEST_SEMANTIC_DSN"] = "postgresql://u:p@h/db"
        try:
            database = DatabaseSemantics(
                name="pg", dialect="postgres", dsn_env="TEST_SEMANTIC_DSN"
            )
            self.assertEqual(database.resolved_dsn(), "postgresql://u:p@h/db")
            self.assertTrue(database.has_connection())
        finally:
            os.environ.pop("TEST_SEMANTIC_DSN", None)


class ValidatorTest(unittest.TestCase):
    """元数据与真实库对不上时必须报错——这是接真实库时最容易踩的坑。"""

    def _layer(self, tables: dict) -> SemanticLayer:
        layer = SemanticLayer(source="test")
        layer.databases["erp_db"] = DatabaseSemantics(
            name="erp_db",
            description="ERP",
            aliases=["ERP库"],
            dialect="sqlite",
            db_path="runtime_data/erp_demo.db",
            tables=tables,
        )
        return layer

    def test_table_missing_from_real_database_is_an_error(self):
        layer = self._layer(
            {"ghost": {"description": "不存在的表", "columns": {"x": {"description": "x"}}}}
        )
        report = validate_semantic_layer(layer, live=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("ghost" in issue.where for issue in report.errors))

    def test_column_missing_from_real_database_is_an_error(self):
        layer = self._layer(
            {
                "supplier": {
                    "description": "供应商",
                    "columns": {"nope_column": {"description": "不存在"}},
                }
            }
        )
        report = validate_semantic_layer(layer, live=True)
        self.assertFalse(report.ok)
        self.assertTrue(
            any("nope_column" in issue.where for issue in report.errors), report.render()
        )

    def test_table_present_in_database_but_undocumented_is_warned(self):
        """漏写的表对模型不存在，涉及它的问题永远答不出来，必须显式提示。"""
        layer = self._layer(
            {
                "supplier": {
                    "description": "供应商",
                    "columns": {"supplier_name": {"description": "名称"}},
                }
            }
        )
        report = validate_semantic_layer(layer, live=True)
        warned = " ".join(issue.where for issue in report.warnings)
        self.assertIn("purchase_order", warned)

    def test_cross_database_link_to_unknown_column_is_an_error(self):
        layer = self._layer(
            {
                "supplier": {
                    "description": "供应商",
                    "columns": {"supplier_name": {"description": "名称"}},
                }
            }
        )
        layer.cross_database_links = [
            {"left": "erp_db.supplier.supplier_name", "right": "ghost_db.t.c"}
        ]
        report = validate_semantic_layer(layer, live=False)
        self.assertFalse(report.ok)

    def test_missing_connection_is_an_error(self):
        layer = SemanticLayer(source="test")
        layer.databases["x"] = DatabaseSemantics(name="x", tables={"t": {"columns": {"c": {}}}})
        report = validate_semantic_layer(layer, live=False)
        self.assertFalse(report.ok)

    def test_inline_dsn_is_warned_as_a_secret_leak(self):
        layer = SemanticLayer(source="test")
        layer.databases["pg"] = DatabaseSemantics(
            name="pg",
            dialect="postgres",
            dsn="postgresql://user:secret@host/db",
            description="pg",
            aliases=["pg"],
            tables={"t": {"description": "t", "columns": {"c": {"description": "c"}}}},
        )
        report = validate_semantic_layer(layer, live=False)
        self.assertTrue(any("版本库" in issue.message for issue in report.warnings))

    def test_offline_mode_skips_live_comparison(self):
        layer = self._layer(
            {"ghost": {"description": "不存在", "columns": {"x": {"description": "x"}}}}
        )
        report = validate_semantic_layer(layer, live=False)
        self.assertTrue(report.ok)
        self.assertEqual(report.live_checked, [])

    def test_unreachable_database_warns_instead_of_failing(self):
        """连不上库是环境问题，不是元数据问题，不该把 CI 卡死。"""
        layer = SemanticLayer(source="test")
        layer.databases["gone"] = DatabaseSemantics(
            name="gone",
            description="d",
            aliases=["a"],
            dialect="sqlite",
            db_path="/nonexistent/path/to.db",
            tables={"t": {"description": "t", "columns": {"c": {"description": "c"}}}},
        )
        report = validate_semantic_layer(layer, live=True)
        self.assertTrue(report.ok, report.render())
        self.assertEqual(report.live_checked, [])


class PipelineIntegrationTest(unittest.TestCase):
    """语义层接进主链路之后，检索与规划拿到的仍然是同一份元数据。"""

    def test_pipeline_uses_semantic_layer_for_metadata(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline

        pipeline = Text2SQLPipeline()
        self.assertEqual(
            pipeline.business_metas, pipeline.semantic.business_metas()
        )
        self.assertEqual(
            [database.name for database in pipeline.databases],
            pipeline.semantic.database_names(),
        )

    def test_pipeline_accepts_an_injected_layer(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline
        from ai_sql_agent.pipeline.objects import PipelineConfig

        layer = demo_semantic_layer()
        layer.databases.pop("mes_db")
        layer.databases.pop("wms_db")

        pipeline = Text2SQLPipeline(PipelineConfig(semantic_layer=layer))
        self.assertEqual(
            [database.name for database in pipeline.databases], ["erp_db", "pm_db"]
        )

    def test_metric_definitions_reach_the_qa_path(self):
        """指标口径以前写死在问答类里，现在必须来自语义层。"""
        from ai_sql_agent.pipeline.data_qa import RuleBasedDataQAClient

        client = RuleBasedDataQAClient()
        self.assertEqual(client.definitions, demo_semantic_layer().metric_definitions)


if __name__ == "__main__":
    unittest.main()


class InjectedLayerConsistencyTest(unittest.TestCase):
    """一个进程里只能有一份口径：查询与解释必须来自同一套元数据。"""

    def test_qa_definitions_follow_the_pipelines_layer(self):
        import tempfile
        from pathlib import Path

        from ai_sql_agent.memory import ConversationMemoryService
        from ai_sql_agent.memory.conversation import MemoryServiceConfig
        from ai_sql_agent.pipeline import DynamicSqlAgentService, Text2SQLPipeline
        from ai_sql_agent.pipeline.objects import PipelineConfig

        layer = demo_semantic_layer()
        layer.metric_definitions = {"自定义指标": "注入的定义"}

        with tempfile.TemporaryDirectory() as temp:
            memory = ConversationMemoryService(
                MemoryServiceConfig(
                    db_path=Path(temp) / "m.db",
                    use_model_summarizer_when_available=False,
                    use_reranker_when_available=False,
                )
            )
            try:
                service = DynamicSqlAgentService(
                    Text2SQLPipeline(PipelineConfig(semantic_layer=layer)),
                    memory,
                    use_model_when_available=False,
                )
                self.assertEqual(
                    service.data_qa_client.definitions, {"自定义指标": "注入的定义"}
                )
            finally:
                memory.close()
