"""
鉴权与数据权限。

内部自用部署不做多租户，但仍然需要回答三个问题：
这条查询是谁发的（审计与配额）、他能查哪些表（数据权限）、
以及他有没有资格代别人查（防止随手改 user_id 读到别人的个人知识库）。
"""

from .enforcement import (
    AuthorizationDecision,
    AuthorizationScope,
    authorization_scope,
    authorize_sql,
    check_sql_access,
    current_scope,
)
from .key_store import ApiKeyStore
from .objects import ApiKeyRecord, DataPolicy, Principal
from .sql_analysis import ColumnRef, SqlAnalysis, TableRef, analyze_sql
from .sql_tables import extract_referenced_tables
from .views import ViewCatalog

__all__ = [
    "ApiKeyRecord",
    "ApiKeyStore",
    "AuthorizationDecision",
    "AuthorizationScope",
    "ColumnRef",
    "DataPolicy",
    "Principal",
    "SqlAnalysis",
    "TableRef",
    "ViewCatalog",
    "analyze_sql",
    "authorization_scope",
    "authorize_sql",
    "check_sql_access",
    "current_scope",
    "extract_referenced_tables",
]
