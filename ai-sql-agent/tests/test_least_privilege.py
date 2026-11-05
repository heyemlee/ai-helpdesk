"""
数据库侧最小授权的生成逻辑。

真实 PostgreSQL 上的验证在 tests/test_authorization_postgres.py——那边证明
「生成的 GRANT 真的挡住了禁列」。这里验的是**生成得对不对**，不需要连库。
"""
from __future__ import annotations

import unittest

from ai_sql_agent.auth import DataPolicy
from ai_sql_agent.ops.least_privilege import (
    LeastPrivilegePlan,
    TableInventory,
    build_plan,
    row_policy_sql,
)

INVENTORY = TableInventory(columns={
    "job": ["job_no", "contract_amount"],
    "batch": ["sku", "unit_cost", "qty"],
    "payroll": ["emp_id", "salary"],
})


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.policy = DataPolicy(
            allow_tables=("erp_db.job", "erp_db.batch"),
            deny_columns=("erp_db.batch.unit_cost",),
        )
        self.plan = build_plan(
            self.policy, INVENTORY, database="erp_db", role="ai_sql_ro"
        )

    def test_denied_table_is_never_granted(self):
        self.assertIn("payroll", self.plan.denied_tables)
        self.assertNotIn("payroll", self.plan.granted_tables)
        self.assertNotIn('GRANT SELECT ON "public"."payroll"', self.plan.to_sql())

    def test_table_without_denied_columns_gets_a_plain_grant(self):
        self.assertIn('GRANT SELECT ON "public"."job"', self.plan.to_sql())

    def test_table_with_denied_columns_gets_a_column_grant(self):
        """
        列级 GRANT 是这条缺口的关键：只读账号只挡写，挡不住 `SELECT unit_cost`。
        没列出来的列在服务端就查不了，应用层漏判也越不了权。
        """
        sql = self.plan.to_sql()
        self.assertIn('GRANT SELECT ("sku", "qty") ON "public"."batch"', sql)
        self.assertNotIn("unit_cost", sql.split("-- ")[0] + sql)
        self.assertEqual(self.plan.column_grants["batch"], ["sku", "qty"])
        self.assertEqual(self.plan.denied_columns["batch"], ["unit_cost"])

    def test_revoke_comes_first(self):
        """先收回再授予，避免继承历史授权。"""
        statements = self.plan.statements()
        revoke = next(i for i, line in enumerate(statements) if line.startswith("REVOKE"))
        grant = next(i for i, line in enumerate(statements) if line.startswith("GRANT"))
        self.assertLess(revoke, grant)

    def test_a_table_with_every_column_denied_is_not_granted_at_all(self):
        policy = DataPolicy(
            allow_tables=("erp_db.payroll",), deny_columns=("erp_db.payroll.*",)
        )
        plan = build_plan(policy, INVENTORY, database="erp_db", role="ro")
        self.assertEqual(plan.column_grants["payroll"], [])
        self.assertNotIn('GRANT SELECT ("', plan.to_sql())

    def test_identifiers_are_validated(self):
        plan = LeastPrivilegePlan(
            role='ro"; DROP TABLE x; --', schema="public",
            granted_tables=[], denied_tables=[], column_grants={},
        )
        with self.assertRaises(ValueError):
            plan.to_sql()


class RowPolicyTest(unittest.TestCase):
    def test_rls_statements_are_complete(self):
        sql = row_policy_sql(
            role="ai_sql_ro", table="batch",
            using_expression="owner_id = current_setting('app.user_id', true)",
        )
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        # FORCE 不能少：表 owner 默认绕过 RLS
        self.assertIn("FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("CREATE POLICY", sql)
        self.assertIn("current_setting", sql)

    def test_policy_is_replaced_not_duplicated(self):
        sql = row_policy_sql(role="ro", table="t", using_expression="true")
        self.assertIn("DROP POLICY IF EXISTS", sql)


if __name__ == "__main__":
    unittest.main()
