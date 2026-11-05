"""
业务库只读账号命令行。

    # 打印将要执行的 SQL（不连库，可以先给 DBA 审）
    python -m ai_sql_agent.ops sql --database ai_sql

    # 直接创建（需要有建角色权限的连接串）
    python -m ai_sql_agent.ops provision \
        --admin-dsn postgresql://postgres:***@db:5432/ai_sql \
        --database ai_sql --host db

    # 体检某个连接串是不是真的只读（只读系统目录，生产库也能跑）
    python -m ai_sql_agent.ops audit --dsn "$AI_SQL_BUSINESS_DSN"

    # 按 API Key 里的 DataPolicy 生成数据库侧最小授权（列级 GRANT）
    python -m ai_sql_agent.ops least-privilege \
        --dsn "$AI_SQL_BUSINESS_DSN" --keys deploy/api_keys.json \
        --role ai_sql_ro --policy estimating --database wms_db

    # 连上受限角色，验证「该拒的真的拒了」
    python -m ai_sql_agent.ops verify-privileges \
        --dsn "$AI_SQL_BUSINESS_DSN" --readonly-dsn "$AI_SQL_RO_DSN" \
        --keys deploy/api_keys.json --role ai_sql_ro \
        --policy estimating --database wms_db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .readonly_role import ReadonlyRoleSpec, audit_readonly, provision_readonly_role


def _now_rfc3339() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _spec_from_args(args) -> ReadonlyRoleSpec:
    return ReadonlyRoleSpec(
        role=args.role,
        database=args.database,
        schema=args.schema,
        password=args.password or "",
        statement_timeout_ms=args.statement_timeout_ms,
    )


def _print_audit(audit) -> bool:
    print(f"账号            : {audit.user}")
    print(f"超级用户        : {'是（危险）' if audit.is_superuser else '否'}")
    print(f"可在 schema 建表: {'是（危险）' if audit.can_create_in_schema else '否'}")
    print(f"有写权限的表    : {len(audit.writable_tables)}")
    print(f"默认只读事务    : {'on' if audit.default_transaction_read_only else 'off'}")
    print(f"statement_timeout: {audit.statement_timeout or '未设置'}")

    problems = audit.problems()
    if problems:
        print("\n发现问题：")
        for item in problems:
            print(f"  ✗ {item}")
        print("\n修复：python -m ai_sql_agent.ops provision --admin-dsn ... 然后改用只读账号的 DSN")
        return False

    print("\n✅ 这个账号是只读的")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai_sql_ops", description="AI SQL Agent 业务库只读账号工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("sql", "打印建只读角色的 SQL，不连库"),
        ("provision", "在业务库上创建/更新只读角色"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--role", default="ai_sql_ro")
        item.add_argument("--database", default="ai_sql")
        item.add_argument("--schema", default="public")
        item.add_argument("--password", default="", help="留空则随机生成")
        item.add_argument("--statement-timeout-ms", type=int, default=10000)
        if name == "provision":
            item.add_argument("--admin-dsn", required=True, help="有建角色权限的连接串")
            item.add_argument("--host", default="localhost", help="用于拼输出的 DSN")
            item.add_argument("--port", type=int, default=5432)

    audit_parser = sub.add_parser("audit", help="体检某个连接串是不是真的只读")
    audit_parser.add_argument("--dsn", required=True)
    audit_parser.add_argument("--schema", default="public")

    # 最小授权：把应用层的 DataPolicy 在数据库侧再落一次。
    # 应用层检查是第二道防线，不能是唯一一道——只读账号只挡写，不挡读密列。
    for name, help_text in (
        ("least-privilege", "按 DataPolicy 生成数据库侧最小授权 SQL（列级 GRANT）"),
        ("verify-privileges", "连上受限角色，验证该拒的真的拒了"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--dsn", required=True, help="读表结构用的连接串")
        item.add_argument("--keys", required=True, help="API Key 配置 JSON")
        item.add_argument("--policy", required=True, help="要落地的角色名（policies 里的键）")
        item.add_argument("--database", required=True, help="策略里的业务库名，如 erp_db")
        item.add_argument("--role", default="ai_sql_ro", help="数据库侧的受限角色")
        item.add_argument("--schema", default="public")
        item.add_argument("--apply", action="store_true",
                          help="直接执行（默认只打印，先给 DBA 审）")
        item.add_argument("--json", action="store_true",
                          help="输出机器可读的 JSON，用于存档留证（见 docs/least-privilege-runbook.md）")
        if name == "verify-privileges":
            item.add_argument("--readonly-dsn", required=True,
                              help="受限角色自己的连接串——要用它去查才算数")
            item.add_argument("--row-scope", action="store_true",
                              help="同时生成行级安全（RLS）语句")

    args = parser.parse_args(argv)

    if args.command == "sql":
        print(_spec_from_args(args).to_sql())
        return 0

    if args.command == "provision":
        spec = provision_readonly_role(args.admin_dsn, _spec_from_args(args))
        print("只读角色已就绪。把下面这行配给 AI SQL Agent：\n")
        print(f'  AI_SQL_BUSINESS_DSN="{spec.dsn(args.host, args.port)}"\n')
        print("密码只在这里显示一次，请存进密钥管理。接着体检一下：\n")
        print(f'  python -m ai_sql_agent.ops audit --dsn "{spec.dsn(args.host, args.port)}"')
        return 0

    if args.command in ("least-privilege", "verify-privileges"):
        return _least_privilege(args)

    return 0 if _print_audit(audit_readonly(args.dsn, schema=args.schema)) else 1


def _load_policy(keys_path: str, role: str):
    import json
    from pathlib import Path

    from ai_sql_agent.auth import DataPolicy

    payload = json.loads(Path(keys_path).read_text(encoding="utf-8"))
    rules = (payload.get("policies") or {}).get(role)
    if rules is None:
        available = sorted((payload.get("policies") or {}).keys())
        raise SystemExit(
            f"{keys_path} 里没有名为 {role!r} 的策略。已有：{'、'.join(available) or '（无）'}"
        )
    return DataPolicy.from_dict(rules)


def _least_privilege(args) -> int:
    from .least_privilege import TableInventory, build_plan, verify_least_privilege

    policy = _load_policy(args.keys, args.policy)
    inventory = TableInventory.from_database(args.dsn, schema=args.schema)
    plan = build_plan(
        policy, inventory,
        database=args.database, role=args.role, schema=args.schema,
    )

    if args.command == "least-privilege":
        statements = [plan.to_sql()]
        from .least_privilege import row_policies_from_policy

        statements.extend(
            row_policies_from_policy(
                policy, inventory,
                database=args.database, role=args.role, schema=args.schema,
            )
        )
        rendered = "\n".join(statements)
        if args.json:
            print(json.dumps({
                "command": "least-privilege",
                "role": args.role, "schema": args.schema, "database": args.database,
                "policy": args.policy,
                "granted_tables": plan.granted_tables,
                "denied_tables": plan.denied_tables,
                "column_grants": plan.column_grants,
                "denied_columns": plan.denied_columns,
                "sql": rendered,
            }, ensure_ascii=False, indent=2))
            return 0
        print(rendered)
        if not args.apply:
            print("-- 以上只是打印。确认无误后加 --apply 执行。")
            return 0
        import psycopg

        conn = psycopg.connect(args.dsn, autocommit=True)
        try:
            conn.execute(rendered)
        finally:
            conn.close()
        print("-- 已执行。接着跑 verify-privileges 确认真的生效了。")
        return 0

    report = verify_least_privilege(args.readonly_dsn, plan)
    if args.json:
        # 存档留证用：跑过没跑过、什么时候跑的、结论是什么，
        # 一份文本输出说不清楚（见 docs/least-privilege-runbook.md）
        print(json.dumps({
            "command": "verify-privileges",
            "checked_at": _now_rfc3339(),
            "role": report.role, "schema": args.schema, "database": args.database,
            "policy": args.policy,
            "enforced": report.enforced,
            "findings": [
                {"kind": f.kind, "target": f.target, "expected": f.expected,
                 "actual": f.actual, "detail": f.detail, "ok": f.ok}
                for f in report.findings
            ],
            "errors": report.errors,
            "problems": report.problems(),
        }, ensure_ascii=False, indent=2))
        return 0 if report.enforced else 1
    print(f"角色            : {report.role}")
    print(f"验证的表 / 列   : {len(report.findings)}")
    problems = report.problems()
    if problems:
        print("\n数据库侧没拦住：")
        for item in problems:
            print(f"  ✗ {item}")
        print("\n应用层检查是第二道防线，不能是唯一一道——先把上面这些补上。")
        return 1
    print("\n✅ 策略禁止的表与列，数据库侧确实拒绝了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
