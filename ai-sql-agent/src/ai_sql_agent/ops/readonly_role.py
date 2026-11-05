from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# 只读角色需要的权限：连接库、用 schema、读表读序列，以及对“以后新建的表”
# 也自动放开读权限（默认权限），否则业务方每加一张表就要来找 DBA。
_PROVISION_TEMPLATE = """\
-- AI SQL Agent 业务库只读账号
-- 用有建角色权限的账号执行（通常是 postgres 或库 owner）

-- 1. 建登录角色
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} LOGIN PASSWORD '{password}';
  ELSE
    ALTER ROLE {role} WITH LOGIN PASSWORD '{password}';
  END IF;
END
$$;

-- 2. 明确收回写权限，避免继承 PUBLIC 上的历史授权
REVOKE ALL ON DATABASE {database} FROM {role};
REVOKE ALL ON SCHEMA {schema} FROM {role};
REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC;

-- 3. 只给读
GRANT CONNECT ON DATABASE {database} TO {role};
GRANT USAGE ON SCHEMA {schema} TO {role};
GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role};
GRANT SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role};

-- 4. 以后新建的表也自动只给读（否则每加一张表都要再来一次）
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema}
  GRANT SELECT ON TABLES TO {role};
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema}
  GRANT SELECT ON SEQUENCES TO {role};

-- 5. 兜底：单条语句超时由服务端强制，应用层配置失效时仍然生效
ALTER ROLE {role} SET statement_timeout = '{statement_timeout_ms}ms';
ALTER ROLE {role} SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE {role} SET default_transaction_read_only = on;
"""


@dataclass
class ReadonlyRoleSpec:
    """只读角色规格。"""

    role: str = "ai_sql_ro"
    database: str = "ai_sql"
    schema: str = "public"
    password: str = ""
    statement_timeout_ms: int = 10000

    def __post_init__(self) -> None:
        for name, value in (
            ("role", self.role),
            ("database", self.database),
            ("schema", self.schema),
        ):
            if not value.replace("_", "").isalnum():
                raise ValueError(f"{name} 只允许字母数字下划线：{value}")
        if not self.password:
            self.password = secrets.token_urlsafe(24)

    def to_sql(self) -> str:
        return _PROVISION_TEMPLATE.format(
            role=self.role,
            database=self.database,
            schema=self.schema,
            password=self.password.replace("'", "''"),
            statement_timeout_ms=self.statement_timeout_ms,
        )

    def dsn(self, host: str = "localhost", port: int = 5432) -> str:
        return (
            f"postgresql://{self.role}:{self.password}@{host}:{port}/{self.database}"
        )


@dataclass
class ReadonlyAudit:
    """只读性体检结果。"""

    user: str
    is_superuser: bool = False
    can_create_in_schema: bool = False
    writable_tables: List[str] = field(default_factory=list)
    default_transaction_read_only: bool = False
    statement_timeout: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def is_readonly(self) -> bool:
        return (
            not self.is_superuser
            and not self.can_create_in_schema
            and not self.writable_tables
            and not self.errors
        )

    def problems(self) -> List[str]:
        issues: List[str] = []
        if self.is_superuser:
            issues.append(f"{self.user} 是超级用户，任何只读约束都拦不住它")
        if self.can_create_in_schema:
            issues.append(f"{self.user} 可以在 schema 里建表")
        if self.writable_tables:
            preview = ", ".join(self.writable_tables[:5])
            more = "…" if len(self.writable_tables) > 5 else ""
            issues.append(
                f"{self.user} 对 {len(self.writable_tables)} 张表有写权限：{preview}{more}"
            )
        issues.extend(self.errors)
        return issues


def provision_readonly_role(admin_dsn: str, spec: ReadonlyRoleSpec) -> ReadonlyRoleSpec:
    """用管理员连接创建/更新只读角色。"""
    import psycopg

    conn = psycopg.connect(admin_dsn, autocommit=True)
    try:
        conn.execute(spec.to_sql())
    finally:
        conn.close()
    return spec


def audit_readonly(dsn: str, schema: str = "public") -> ReadonlyAudit:
    """
    体检当前账号是不是真的只读。

    全部走系统目录查询，不写任何数据——生产库上跑也安全。
    """
    import psycopg

    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    try:
        user = conn.execute("SELECT current_user").fetchone()[0]
        audit = ReadonlyAudit(user=user)

        audit.is_superuser = bool(
            conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            ).fetchone()[0]
        )
        audit.can_create_in_schema = bool(
            conn.execute(
                "SELECT has_schema_privilege(current_user, %s, 'CREATE')", (schema,)
            ).fetchone()[0]
        )
        audit.writable_tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                  AND (
                    has_table_privilege(current_user, quote_ident(table_schema) || '.' || quote_ident(table_name), 'INSERT')
                    OR has_table_privilege(current_user, quote_ident(table_schema) || '.' || quote_ident(table_name), 'UPDATE')
                    OR has_table_privilege(current_user, quote_ident(table_schema) || '.' || quote_ident(table_name), 'DELETE')
                    OR has_table_privilege(current_user, quote_ident(table_schema) || '.' || quote_ident(table_name), 'TRUNCATE')
                  )
                ORDER BY table_name
                """,
                (schema,),
            ).fetchall()
        ]
        audit.default_transaction_read_only = (
            conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        )
        audit.statement_timeout = conn.execute("SHOW statement_timeout").fetchone()[0]
        return audit
    except Exception as exc:
        return ReadonlyAudit(user="unknown", errors=[f"体检失败：{exc}"])
    finally:
        conn.close()


def audit_business_databases(databases: Sequence, schema: str = "public") -> List[ReadonlyAudit]:
    """对所有 PostgreSQL 业务库做只读体检（SQLite 库跳过）。"""
    results: List[ReadonlyAudit] = []
    for database in databases:
        if getattr(database, "dialect", "sqlite") != "postgres":
            continue
        dsn: Optional[str] = getattr(database, "dsn", "")
        if not dsn:
            continue
        results.append(audit_readonly(dsn, schema=schema))
    return results
