"""Controlled projection corrections; no real model verdicts or archived plans."""
import sqlite3
import unittest
from unittest.mock import Mock

from ai_sql_agent.generation.objects import CotStep, LocalSchema, SqlGenerationRequest, TableColumn, TableSchema
from ai_sql_agent.generation.projection import RawProjectionError, projection_issues
from ai_sql_agent.generation.schema_store import LocalSchemaStore
from ai_sql_agent.generation.sql_generator import SqlGenerator


class BusinessProjectionTest(unittest.TestCase):
    def setUp(self):
        self.store = LocalSchemaStore({'shipment': TableSchema('shipment', [
            TableColumn('parent_ref', '所属业务编号'), TableColumn('line_name', '明细名称'),
            TableColumn('amount', '数量'), TableColumn('state', '状态'),
            TableColumn('internal_id', '内部流水编号'),
        ])}, [])
        self.step = CotStep('sample', 'shipment(parent_ref,line_name,amount,state)',
                            "按 parent_ref='P-41' 筛选，逐条返回明细", 'line_name,amount,state')
        self.query = '请提供 P-41 各项明细的数量及状态。'
        self.missing = "SELECT line_name,amount,state FROM shipment WHERE parent_ref='P-41'"
        self.correct = "SELECT parent_ref,line_name,amount,state FROM shipment WHERE parent_ref='P-41'"

    def issues(self, sql, step=None, query=None):
        step = step or self.step
        return projection_issues(sql, step, LocalSchema(tables=self.store.tables), 'sqlite',
                                 user_query=self.query if query is None else query)

    def test_omitted_parent_binding_is_corrected_without_changing_detail_grain_or_nulls(self):
        self.assertTrue(self.issues(self.missing))
        coder = Mock()
        coder.generate_sql.side_effect = [self.missing, self.correct]
        coder.clean_sql.side_effect = lambda sql: sql
        result = SqlGenerator(self.store, coder_client=coder).generate(self.step, user_query=self.query)
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript("""CREATE TABLE shipment(parent_ref TEXT,line_name TEXT,amount INTEGER,state TEXT);
            INSERT INTO shipment VALUES('P-41','hinge',3,'pending');
            INSERT INTO shipment VALUES('P-41','panel',NULL,'processing');
            INSERT INTO shipment VALUES('P-42','hinge',9,'resolved');""")
        self.assertEqual(db.execute(result.sql).fetchall(), [('P-41','hinge',3,'pending'),('P-41','panel',None,'processing')])
        self.assertEqual(coder.generate_sql.call_count, 2)
        self.assertIn(self.query, coder.generate_sql.call_args_list[0].args[0])
        self.assertIn('parent_ref', coder.generate_sql.call_args.args[0])
        self.assertNotIn('internal_id', result.sql)

    def test_existing_complete_query_is_not_rewritten_or_regenerated(self):
        coder = Mock()
        coder.generate_sql.return_value = self.correct
        coder.clean_sql.side_effect = lambda sql: sql
        result = SqlGenerator(self.store, coder_client=coder).generate(self.step, user_query=self.query)
        self.assertEqual(result.sql, self.correct)
        self.assertEqual(coder.generate_sql.call_count, 1)

    def test_name_or_constant_cannot_substitute_for_parent_business_column(self):
        for sql in [self.missing, self.missing.replace('SELECT ', "SELECT 'P-41' AS parent_ref,"),
                    self.missing.replace('SELECT ', 'SELECT NULL AS parent_ref,')]:
            with self.subTest(sql=sql): self.assertTrue(self.issues(sql))
        coder = Mock()
        coder.generate_sql.return_value = self.missing
        coder.clean_sql.side_effect = lambda sql: sql
        with self.assertRaises(RawProjectionError):
            SqlGenerator(self.store, coder_client=coder).generate(self.step, user_query=self.query)
        self.assertEqual(coder.generate_sql.call_count, 2)

    def test_explicit_ordinary_columns_and_aliases_are_checked_without_raw_marker(self):
        step = CotStep('sample', 'shipment', '逐条输出', 'parent_ref AS reference,amount,state')
        for sql in ['SELECT parent_ref,amount,state FROM shipment',
                    'SELECT parent_ref AS reference,amount FROM shipment',
                    "SELECT 'P-41' AS reference,amount,state FROM shipment"]:
            with self.subTest(sql=sql): self.assertTrue(self.issues(sql, step, query=''))
        self.assertEqual(self.issues('SELECT parent_ref AS reference,amount,state FROM shipment', step, query=''), [])

    def test_aggregate_distinct_and_computed_outputs_keep_requested_grain(self):
        for target,sql in [
            ('SUM(amount) AS total', "SELECT SUM(amount) AS total FROM shipment WHERE parent_ref='P-41'"),
            ('line_name', "SELECT DISTINCT line_name FROM shipment WHERE parent_ref='P-41'"),
            ('amount*2 AS doubled', "SELECT parent_ref,amount*2 AS doubled FROM shipment WHERE parent_ref='P-41'"),
            ('line_name,SUM(amount) AS total', "SELECT line_name,SUM(amount) AS total FROM shipment WHERE parent_ref='P-41' GROUP BY line_name"),
        ]:
            step = CotStep('sample', 'shipment', '按所问粒度计算', target)
            with self.subTest(target=target): self.assertEqual(self.issues(sql, step), [])
        step = CotStep('sample', 'shipment', '逐条计算数量', 'amount*2 AS doubled')
        self.assertTrue(self.issues("SELECT amount*2 AS doubled FROM shipment WHERE parent_ref='P-41'", step))

    def test_internal_id_and_unknown_metadata_do_not_create_business_requirements(self):
        step = CotStep('sample', 'shipment', "按 internal_id='P-41' 筛选", 'state')
        self.assertEqual(self.issues("SELECT state FROM shipment WHERE internal_id='P-41'", step), [])
        self.assertEqual(self.issues(self.missing, query='查询另一个编号 P-42 的状态'), [])
        local = self.store.extract_local_schema(self.step)
        local.tables['shipment'].columns[0].description = ''
        self.assertEqual(projection_issues(self.missing,self.step,local,'sqlite',user_query=self.query), [])

    def test_legacy_third_positional_generation_request_argument_is_still_dialect(self):
        request = SqlGenerationRequest(self.step,self.store.extract_local_schema(self.step),'postgresql')
        self.assertEqual(request.sql_dialect,'postgresql')
        self.assertEqual(request.user_query,'')

    def test_chinese_adjacent_identifiers_match_without_matching_a_longer_english_identifier(self):
        self.assertTrue(self.issues(self.missing, query='查业务P-41的明细状态'))
        self.assertEqual(self.issues(self.missing, query='Look up XP-41 or P-410 status'), [])

    def test_alias_named_like_another_real_column_is_only_a_header(self):
        step = CotStep('sample','shipment','逐条输出','parent_ref AS state')
        self.assertEqual(self.issues('SELECT parent_ref AS state FROM shipment',step,query=''), [])
        self.assertTrue(self.issues('SELECT state FROM shipment',step,query=''))
        step.output_target = 'parent_ref（别名state）'
        self.assertEqual(self.issues('SELECT parent_ref AS state FROM shipment',step,query=''), [])

    def test_cross_database_correction_retains_the_original_query(self):
        from ai_sql_agent.pipeline import Text2SQLPipeline
        pipeline = Text2SQLPipeline.__new__(Text2SQLPipeline)
        pipeline._tables_of = Mock(return_value={'shipment'})
        generator = Mock()
        generator.generate.return_value = type('Result',(),{'sql':self.correct})()
        pipeline._enforce_step_database(sql_result=type('Result',(),{'sql':'SELECT * FROM foreign_table'})(),
            cot_step=self.step,generator=generator,dialect='sqlite',previous_results='',step_index=1,user_query=self.query)
        self.assertEqual(generator.generate.call_args.kwargs['user_query'],self.query)

    def test_plan_crop_cannot_hide_the_selected_tables_real_business_identifier(self):
        step = CotStep('sample','shipment','逐条查询状态','state')
        coder = Mock()
        coder.generate_sql.side_effect = ["SELECT state FROM shipment WHERE parent_ref='P-41'",
                                          "SELECT parent_ref,state FROM shipment WHERE parent_ref='P-41'"]
        coder.clean_sql.side_effect = lambda sql: sql
        result = SqlGenerator(self.store,coder_client=coder).generate(step,user_query=self.query)
        self.assertEqual(coder.generate_sql.call_count,2)
        first = coder.generate_sql.call_args_list[0].args[0]
        self.assertIn('parent_ref：所属业务编号',first)
        self.assertNotIn('internal_id：',first)
        self.assertEqual(result.sql,"SELECT parent_ref,state FROM shipment WHERE parent_ref='P-41'")

    def test_simple_select_and_explicit_value_only_requests_do_not_imply_a_detail_list(self):
        for instruction,query in [('查询状态','查看 P-41 的状态值'),
                                  ('逐条明细','只要P-41的状态值，不要编号或明细列表'),
                                  ('逐条明细','Only the status value for P-41, without identifiers')]:
            step = CotStep('sample','shipment',instruction,'state')
            sql = "SELECT state FROM shipment WHERE parent_ref='P-41'"
            with self.subTest(query=query): self.assertEqual(self.issues(sql,step,query=query), [])

    def test_real_nullable_attribute_is_a_column_but_constant_null_is_not(self):
        step = CotStep('sample','shipment','属性取值','state')
        query = '只要P-41的状态值，不要编号'
        sql = "SELECT state FROM shipment WHERE parent_ref='P-41'"
        self.assertEqual(self.issues(sql,step,query=query), [])
        self.assertTrue(self.issues(sql.replace('SELECT state','SELECT NULL AS state'),step,query=query))
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript("CREATE TABLE shipment(parent_ref TEXT,state TEXT); INSERT INTO shipment VALUES('P-41',NULL);")
        self.assertEqual(db.execute(sql).fetchall(), [(None,)])
