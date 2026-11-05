from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .identifiers import bind_dialect, fold, quoted_names_are_case_sensitive


def _glob_exact(target: str, rule: str) -> bool:
    """大小写必须一致。`fnmatchcase` 不随操作系统变（`fnmatch` 在 Windows 上会忽略大小写）。"""
    return fnmatch.fnmatchcase(target, rule)


def _glob_folded(target: str, rule: str) -> bool:
    """ASCII 字母不区分大小写——SQLite 这类库里，那本来就是同一个名字。"""
    return fnmatch.fnmatchcase(fold(target), fold(rule))


def _glob_wide(target: str, rule: str) -> bool:
    """任何大小写都算命中。拒绝规则与行范围规则用这个：宁可多拦一张同名的表。"""
    return fnmatch.fnmatchcase(target.casefold(), rule.casefold())


ALL_ROWS = "*"
"""`RowFilter.predicate` 的哨兵值：**显式声明**这张表不做行隔离。"""


@dataclass(frozen=True)
class RowFilter:
    """
    一条行范围规则（设计见 docs/authz-model.md）。

    `table` 是 `库.表` 的 glob；`predicate` 是一段 SQL 布尔表达式，落到
    PostgreSQL 的 RLS 上，例如 `owner_id = current_setting('app.user_id', true)`。
    `predicate` 为 `"*"` 表示**显式**放行所有行——注意它和「没配」不是一回事，
    见 `DataPolicy.row_predicate` 的文档。
    """

    table: str
    predicate: str = ALL_ROWS

    @property
    def unrestricted(self) -> bool:
        return self.predicate.strip() == ALL_ROWS

    def matches(self, database: str, table: str) -> bool:
        # 行范围是一种限制，和拒绝规则同一个方向：不区分大小写，宁可多套一张表
        return _glob_wide(f"{database}.{table}", self.table)

    @classmethod
    def from_dict(cls, payload: dict) -> "RowFilter":
        table = str(payload.get("table") or "").strip()
        if not table:
            raise ValueError("row_filters 的每一条都要有 table")
        return cls(table=table, predicate=str(payload.get("predicate") or ALL_ROWS))


@dataclass(frozen=True)
class DataPolicy:
    """
    数据权限策略：某个角色能看哪些库表、不能看哪些列。

    内部自用场景下真正要拦的不是租户之间的越界，而是「估价员不能看
    批次成本单价」这类表级/列级授权。规则用 glob 匹配，写法：

        allow_tables = ["erp_db.*", "mes_db.work_order"]
        deny_tables  = ["hr_db.*"]
        deny_columns = ["wms_db.batch.unit_cost", "*.*.id_card_no"]

    deny 优先于 allow。

    名字的大小写怎么比，见 `ai_sql_agent.auth.identifiers`，一句话：**拒绝从宽、放行从严**。
    拒绝规则在任何库上都不区分大小写；放行规则在 SQLite 上不区分大小写，
    在 PostgreSQL 上必须与存储名大小写一致（那里 `"Orders"` 与 `orders` 是两张表）。
    哪个库按哪种规则，由调用方用 `for_dialect()` 绑定——目录、检索裁剪、SQL 授权关卡
    都是这么做的，所以三处给出的是同一个答案。
    """

    allow_tables: Tuple[str, ...] = ("*",)
    deny_tables: Tuple[str, ...] = ()
    deny_columns: Tuple[str, ...] = ()

    row_filters: Tuple[RowFilter, ...] = ()
    """
    行范围规则，按声明顺序取**第一条匹配**。空 = 这份策略没有声明行范围。

    「没声明」和「声明了放行所有行」是两件事，见 `row_predicate`。
    """

    case_sensitive_allow: bool = False
    """
    放行规则是否要求大小写一致。**不是配置项**，配置文件里不认这个键——
    它由 `for_dialect()` 按目标库的方言设置（PostgreSQL 为真）。
    """

    def for_dialect(self, dialect: str) -> "DataPolicy":
        """这份策略在某个库上的视图：按那个库的大小写规则匹配放行规则。"""
        exact = quoted_names_are_case_sensitive(dialect)
        if exact == self.case_sensitive_allow:
            return self
        return replace(self, case_sensitive_allow=exact)

    def _allowed_by_rule(self, target: str) -> bool:
        match = _glob_exact if self.case_sensitive_allow else _glob_folded
        return any(match(target, rule) for rule in self.allow_tables)

    def allows_table(self, database: str, table: str) -> bool:
        target = f"{database}.{table}"
        if any(_glob_wide(target, rule) for rule in self.deny_tables):
            return False
        return self._allowed_by_rule(target)

    def allows_column(self, database: str, table: str, column: str) -> bool:
        if not self.allows_table(database, table):
            return False
        target = f"{database}.{table}.{column}"
        return not any(_glob_wide(target, rule) for rule in self.deny_columns)

    def allows_table_reference(self, database: str, names: Sequence[str]) -> bool:
        """
        判定一次**表引用**。`names` 是它可能被叫到的所有名字：
        `("orders",)`，或者带 schema 时 `("orders", "secret.orders")`。

        规则是「任一名字命中 deny 就拒，任一名字命中 allow 才放」：

        - `erp_db.*` 放行两个 schema 里的 orders —— `*` 本来就是这个意思；
        - `erp_db.secret.orders` **只**放行 secret 那张，public 那张不沾光；
        - `erp_db.orders`（不带 schema 的老规则）继续放行 orders，
          **包括别的 schema 里的同名表**——一条不带 schema 的规则说的就是
          「叫 orders 的那张表」，这是它一直以来的含义，不在这次改动里悄悄收紧；
          要区分 schema 就写带 schema 的规则，或者加一条 `deny_tables`。

        写成独立方法而不是 `all(allows_table(...))`：后者会让「只写限定名的策略」
        永远拒绝一切（裸名对不上限定名的 pattern），2026-09-04 在真实 PostgreSQL
        上验出来的就是这个。
        """
        candidates = [f"{database}.{name}" for name in names]
        if any(
            _glob_wide(target, rule)
            for target in candidates
            for rule in self.deny_tables
        ):
            return False
        return any(self._allowed_by_rule(target) for target in candidates)

    def allows_column_reference(
        self, database: str, names: Sequence[str], column: str
    ) -> bool:
        """列级同理：任一名字命中 deny_columns 就拒。"""
        if not self.allows_table_reference(database, names):
            return False
        return not any(
            _glob_wide(f"{database}.{name}.{column}", rule)
            for name in names
            for rule in self.deny_columns
        )

    def restricts_columns(self, database: str, table: str) -> bool:
        """
        这张表上有没有列级限制？

        执行器那一层没有 schema，展不开 `SELECT *`，只能反过来问这个问题——
        有禁列就不许写通配符。判定规则见 `enforcement.restricts_columns`。
        """
        if not self.allows_table(database, table):
            return True
        from .enforcement import _rules_restrict_columns

        return _rules_restrict_columns(self.deny_columns, database, table)

    def is_unrestricted(self) -> bool:
        """
        这份策略有没有隐藏任何东西：放行规则里有通配 `*`，且没有任何 deny。

        授权关卡用它判断视图目录要不要介入——没有东西可隐藏的策略，视图遮不住什么
        （见 `ai_sql_agent.auth.views`）。写得保守：`erp_db.*` 这种只放行一个库的规则
        不算无限制。
        """
        return "*" in self.allow_tables and not self.deny_tables and not self.deny_columns

    def row_predicate(self, database: str, table: str) -> Optional[str]:
        """
        这张表的行过滤谓词。

        三种返回值，**必须分清**：

        | 返回 | 含义 |
        | --- | --- |
        | `None` | **没有声明过**。运行时按放行处理（不破坏现网），但生产模式启动检查会拒 |
        | `""` | 显式声明放行所有行（`predicate: "*"`） |
        | 一段表达式 | 按它过滤 |

        为什么不让「没声明」直接等于「拒绝所有行」：现有部署一条 `row_filters`
        都没有，那样翻上去服务立刻不可用。也不让它等于「放行所有行」：
        那就是把 `full_access` 的老毛病换个维度复发——漏配一张表就是全表可见，
        而且没有症状。所以运行时宽松、**配置校验时严格**，宽松必须是有人写下来的。
        """
        for rule in self.row_filters:
            if rule.matches(database, table):
                return "" if rule.unrestricted else rule.predicate
        return None

    def merge(self, other: "DataPolicy") -> "DataPolicy":
        """多角色取并集：allow 合并，deny 也合并（deny 始终优先）。"""
        return DataPolicy(
            allow_tables=tuple(dict.fromkeys(self.allow_tables + other.allow_tables)),
            deny_tables=tuple(dict.fromkeys(self.deny_tables + other.deny_tables)),
            deny_columns=tuple(dict.fromkeys(self.deny_columns + other.deny_columns)),
            row_filters=tuple(dict.fromkeys(self.row_filters + other.row_filters)),
            case_sensitive_allow=self.case_sensitive_allow or other.case_sensitive_allow,
        )

    @classmethod
    def full_access(cls) -> "DataPolicy":
        return cls()

    @classmethod
    def deny_all(cls) -> "DataPolicy":
        """
        一张表都不给。`allow_tables` 为空时 `allows_table` 的 `any(...)` 恒为 False。

        这是「没有配置策略的角色」的默认值——见 `_resolve_policy`。
        """
        return cls(allow_tables=())

    @classmethod
    def from_dict(cls, payload: dict) -> "DataPolicy":
        return cls(
            allow_tables=tuple(payload.get("allow_tables", ["*"])),
            deny_tables=tuple(payload.get("deny_tables", [])),
            deny_columns=tuple(payload.get("deny_columns", [])),
            row_filters=tuple(
                RowFilter.from_dict(item) for item in payload.get("row_filters", [])
            ),
        )


@dataclass(frozen=True)
class IntersectionPolicy:
    """
    两份策略的交集：**服务身份能做的 ∩ 最终用户能做的**（设计见 docs/authz-model.md）。

    只换 `user_id` 的 impersonation 会保留服务密钥的角色与策略，
    所以 alice 和 bob 权限完全相同——一把服务 Key 替所有员工发请求，
    每个员工拿到的都是**服务身份的全部权限**。

    交集不是二选一：服务 Key 的策略是「这个应用最多能碰什么」，最终用户的策略是
    「这个人最多能碰什么」，两者都是上界，都要守。

    **刻意不做 glob 代数**（把两份 allow_tables 求交算出一份新的 glob 列表）：
    既复杂又容易错。组合对象把判定推迟到每次询问时做，语义上等价而且不可能算错。
    它满足 SchemaAccessFilter 协议加上授权关卡用的那几个扩展方法，
    所以对 `enforcement.py` 完全透明。
    """

    service: Any
    user: Any

    def for_dialect(self, dialect: str) -> "IntersectionPolicy":
        """两边各自绑到这个库的方言上，交集的语义不变。"""
        return IntersectionPolicy(
            service=bind_dialect(self.service, dialect),
            user=bind_dialect(self.user, dialect),
        )

    def allows_table(self, database: str, table: str) -> bool:
        return self.service.allows_table(database, table) and self.user.allows_table(
            database, table
        )

    def allows_column(self, database: str, table: str, column: str) -> bool:
        return self.service.allows_column(
            database, table, column
        ) and self.user.allows_column(database, table, column)

    def allows_table_reference(self, database: str, names: Sequence[str]) -> bool:
        return _ask(
            self.service, "allows_table_reference", database, names
        ) and _ask(self.user, "allows_table_reference", database, names)

    def allows_column_reference(
        self, database: str, names: Sequence[str], column: str
    ) -> bool:
        return _ask(
            self.service, "allows_column_reference", database, names, column
        ) and _ask(self.user, "allows_column_reference", database, names, column)

    def is_unrestricted(self) -> bool:
        """两边都没有隐藏任何东西才算无限制（任一边没声明这个方法就当有限制）。"""
        return all(
            callable(getattr(side, "is_unrestricted", None)) and side.is_unrestricted()
            for side in (self.service, self.user)
        )

    def restricts_columns(self, database: str, table: str) -> bool:
        """任一边有列级限制就算有——取更严的那个。"""
        return bool(
            self.service.restricts_columns(database, table)
            or self.user.restricts_columns(database, table)
        )

    def row_predicate(self, database: str, table: str) -> Optional[str]:
        """两边的谓词 AND 起来。任一边没声明就只用另一边的。"""
        parts = [
            predicate
            for predicate in (
                self.service.row_predicate(database, table),
                self.user.row_predicate(database, table),
            )
            if predicate
        ]
        if not parts:
            # 都没声明或都显式全放：把「有没有声明过」如实传下去
            declared = [
                self.service.row_predicate(database, table),
                self.user.row_predicate(database, table),
            ]
            return "" if any(item == "" for item in declared) else None
        if len(parts) == 1:
            return parts[0]
        return " AND ".join(f"({item})" for item in parts)


def _ask(policy: Any, method: str, database: str, names, *rest) -> bool:
    """扩展方法可选：没实现就退回逐个名字问基础方法（方向保守）。"""
    hook = getattr(policy, method, None)
    if callable(hook):
        return bool(hook(database, names, *rest))
    if rest:
        return all(policy.allows_column(database, name, rest[0]) for name in names)
    return all(policy.allows_table(database, name) for name in names)


@dataclass(frozen=True)
class Principal:
    """
    一次请求的调用方身份。

    内部自用不做多租户，但仍要知道「这条查询是谁发起的」：
    审计要记、配额要按人算、数据权限要按角色判。
    """

    caller: str
    """调用方标识（应用名或人名），来自 API Key 配置。"""

    user_id: str
    """记忆与配额的归属人。"""

    roles: Tuple[str, ...] = ()
    policy: DataPolicy = field(default_factory=DataPolicy.full_access)

    can_impersonate: bool = False
    """
    是否允许代其他 user_id 发起查询。

    内部 BI 门户这类服务端调用方需要（它替所有员工发请求），
    个人脚本用的 Key 不应该开。
    """

    is_admin: bool = False
    """是否可以调用运维接口（如手动重建 Schema 索引）。"""

    rate_limit_per_minute: int = 60
    daily_query_quota: int = 0
    """0 表示不限制。"""

    daily_token_quota: int = 0

    anonymous: bool = False
    """鉴权关闭时使用的占位身份。"""

    @classmethod
    def anonymous_principal(cls, user_id: str = "anonymous") -> "Principal":
        return cls(
            caller="anonymous",
            user_id=user_id,
            roles=("anonymous",),
            policy=DataPolicy.full_access(),
            can_impersonate=True,
            is_admin=True,
            anonymous=True,
        )


@dataclass(frozen=True)
class ApiKeyRecord:
    """一条 API Key 配置。"""

    key: str
    caller: str
    user_id: str = ""
    roles: Tuple[str, ...] = ()
    can_impersonate: bool = False
    is_admin: bool = False
    rate_limit_per_minute: int = 60
    daily_query_quota: int = 0
    daily_token_quota: int = 0

    def to_principal(
        self,
        requested_user_id: str,
        policies: Optional[Dict[str, DataPolicy]] = None,
        unknown_role_policy: Optional[DataPolicy] = None,
    ) -> Principal:
        """把 Key 配置 + 请求里的 user_id 解析成本次请求的身份。"""
        resolved_policy = _resolve_policy(
            self.roles, policies or {}, fallback=unknown_role_policy
        )
        return Principal(
            caller=self.caller,
            user_id=requested_user_id or self.user_id or self.caller,
            roles=self.roles,
            policy=resolved_policy,
            can_impersonate=self.can_impersonate,
            is_admin=self.is_admin,
            rate_limit_per_minute=self.rate_limit_per_minute,
            daily_query_quota=self.daily_query_quota,
            daily_token_quota=self.daily_token_quota,
        )


def _resolve_policy(
    roles: Sequence[str],
    policies: Dict[str, DataPolicy],
    fallback: Optional[DataPolicy] = None,
) -> DataPolicy:
    """
    把多个角色的策略合并。**没有匹配到任何策略时 deny by default。**

    这里原来写的是 `if not matched: return full_access()`，注释是「没有配置
    策略的角色视为全放开」。用一个根本不存在的角色就能拿到全部
    权限——配错一个角色名、或者新加一个角色忘了配策略，后果就是全权限，而且
    没有任何症状。安全默认值必须是拒绝：配漏了要表现为「查不到」，不是「都能查」。

    `fallback` 只给显式开了宽松开关的开发环境用（`AI_SQL_UNKNOWN_ROLE_POLICY`），
    生产模式下 `ai_sql_agent.api.app` 启动时就会拒绝那个开关。
    """
    if not roles:
        # 一个角色都没声明：这不是「未知角色」，是「这把 Key 不做角色限制」，
        # 而且只能是人手写出来的——打错角色名产生不了空列表。所以它是显式配置，
        # 不是默认值兜底。生产模式下仍然会在启动时被拒（见
        # `ai_sql_agent.api.app.check_production_readiness`），那里要求每把 Key 都有角色。
        return DataPolicy.full_access()

    matched: List[DataPolicy] = [
        policies[role] for role in roles if role in policies
    ]
    if not matched:
        # 声明了角色，却一条策略都没匹配上——**这才是复现过的那个洞**。
        # 「打错角色名」和「新加角色忘了配策略」都长这个样子，而旧代码把它翻译成
        # 全放开：配置错误静默升级成全部权限，没有任何症状。
        return fallback if fallback is not None else DataPolicy.deny_all()

    merged = matched[0]
    for policy in matched[1:]:
        merged = merged.merge(policy)
    return merged
