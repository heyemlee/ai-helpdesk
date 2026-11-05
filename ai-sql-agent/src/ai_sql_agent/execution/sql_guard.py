"""
执行器的只读校验：这条 SQL 是不是一条、而且只是一条只读查询。

2026-10 之前这里是关键字扫描：正则去掉注释和单引号字符串，剩下的词和一张
禁用词表求交集。它的判断依据是「词」，而只读与否是**结构**问题，于是两头都错：

- 漏放：词法和数据库不一致的地方都能藏东西。`SELECT '--' AS x; DROP TABLE t`
  ——正则先删 `--` 到行尾，把后半条语句当注释删掉了；PostgreSQL 的
  `$$ ... $$` 字符串、嵌套注释同理。`set_config(...)`、`pg_sleep(...)` 这类
  有副作用的函数也不在词表里。
- 误杀：引号里的标识符（`"Last Update"`）、`replace()` 这样的普通函数，
  只要撞上词表就拒。

现在的判定依据是 sqlglot 按方言解析出来的语法树（与授权那条路
`auth/sql_analysis.py` 用同一个解析器、同一套方言映射）：

1. 解析不了就拒（fail closed）。「看不懂」不等于「没问题」。
2. 恰好一条语句；根节点必须是查询（SELECT / 集合运算 / VALUES）。
3. 整棵树里不许出现写操作、DDL、事务与会话控制、`SELECT INTO`、行锁——
   包括藏在 CTE、子查询里的。
4. 函数按语法树里的函数节点判，名单见 `_FORBIDDEN_FUNCTIONS`。

旧的关键字表没有删，降为解析之后的**附加防线**（`_token_backstop`）：它扫的是
sqlglot 按方言切出来的 token，字符串、注释、带引号的标识符天然不在里面，所以
不会再误伤引号里的词；它防的是「语法树把某种写法认成了无害节点」这类解析器
自身的盲区。语法树已经把某个词认成了列名 / 别名 / 表名（`name AS copy`、
`call = 1`）时，这个词就是被解释清楚的，不算命中——`copy`、`call`、`update`
在 PostgreSQL 里都是合法的非保留字，业务库里真有这样的列。多出来的、语法树
没认领的裸词才算。附加防线只能多拒，不能放行语法树已经拒掉的语句。

这一层是应用层防线，与数据库层的只读连接 / 只读账号配合形成纵深防御。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

# 调用方没说方言时，两种都要判过（见 `check_readonly_sql`）。
_DEFAULT_DIALECTS: Tuple[str, ...] = ("sqlite", "postgres")

# 附加防线的关键字表：解析通过的只读查询里，这些词不该以「裸词」出现，
# 除非语法树把它认领成了普通标识符（见 `_token_backstop`）。
# 与旧版相比去掉了 `replace`——它也是常用的字符串函数，`REPLACE INTO` /
# `INSERT OR REPLACE` 由语法树判（前者落成 Command，后者是 Insert）。
_FORBIDDEN_KEYWORDS: Set[str] = {
    "insert",
    "update",
    "delete",
    "merge",
    "upsert",
    "drop",
    "create",
    "alter",
    "truncate",
    "grant",
    "revoke",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "analyze",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "copy",
    "call",
    "do",
    "set",
    "into",
}

# 在 SQLite 与 PostgreSQL 里都是保留字、不加引号做不了标识符的词：语法树即便
# 把它们认成别名，数据库也只会报语法错。这几个不给「被认领」的豁免，
# 宁可多拒（`SELECT 1 AS into` 本来就跑不起来）。
_ALWAYS_KEYWORDS: Set[str] = {"into", "create"}

# 能读写服务器文件、执行任意 SQL 文本、改会话状态或干扰别的会话的函数。
# 名字按小写比；`_FORBIDDEN_FUNCTION_PREFIXES` 覆盖成族出现的函数。
_FORBIDDEN_FUNCTIONS: Set[str] = {
    # SQLite：加载扩展、读写文件（fileio / CLI 扩展），fts3_tokenizer 能拿到函数指针
    "load_extension",
    "writefile",
    "readfile",
    "edit",
    "fsdir",
    "zipfile",
    "fts3_tokenizer",
    # PostgreSQL：改配置项——`set_config('app.user_id', …)` 能在事务里换掉
    # 行级安全看的身份
    "set_config",
    # PostgreSQL：序列写入（只读事务里数据库也会拒，这里提前拦）
    "nextval",
    "setval",
    # PostgreSQL：把 SQL 文本或表名交给数据库执行——授权解析看不到里面的表
    "ts_stat",
    "ts_rewrite",
    "connectby",
    # PostgreSQL：信号与运维
    "pg_cancel_backend",
    "pg_terminate_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_promote",
    "pg_notify",
    "pg_import_system_collations",
    "pg_log_backend_memory_contexts",
    "pg_create_restore_point",
    "pg_start_backup",
    "pg_stop_backup",
    "pg_logdir_ls",
    "pg_stat_statements_reset",
    "loread",
    "lowrite",
}

_FORBIDDEN_FUNCTION_PREFIXES: Tuple[str, ...] = (
    # 服务器文件：pg_read_file / pg_read_binary_file / pg_ls_dir / pg_ls_waldir …
    "pg_read_",
    "pg_ls_",
    "pg_stat_file",
    "pg_file_",  # adminpack：pg_file_write / pg_file_unlink …
    # 大对象：lo_import / lo_export / lo_get / lo_put / lo_from_bytea …
    "lo_",
    # 跨库执行：dblink / dblink_exec / dblink_connect …
    "dblink",
    # 执行查询文本或整表导出：query_to_xml / table_to_xml / cursor_to_xml …
    "query_to_xml",
    "table_to_xml",
    "cursor_to_xml",
    "schema_to_xml",
    "database_to_xml",
    "crosstab",
    # 阻塞与锁
    "pg_sleep",
    "pg_advisory_",
    "pg_try_advisory_",
    # 复制、WAL、备份、统计重置
    "pg_create_",
    "pg_drop_",
    "pg_copy_",
    "pg_replication_",
    "pg_logical_",
    "pg_switch_",
    "pg_wal_",
    "pg_backup_",
    "pg_stat_reset",
)

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class SqlGuardResult:
    """只读校验结果。"""

    allowed: bool
    reason: str = ""
    normalized_sql: str = ""


def strip_sql_noise(sql: str) -> str:
    """
    去掉注释与字符串字面量，只留下骨架。

    只用来生成 `normalized_sql`，**不参与放行判断**：它的词法与数据库不一致
    （见模块说明），拿它做安全判断正是旧实现的问题。
    """
    without_comments = _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))
    without_strings = _STRING_LITERAL.sub(" '' ", without_comments)
    return re.sub(r"\s+", " ", without_strings).strip()


def check_readonly_sql(sql: str, dialect: str = "") -> SqlGuardResult:
    """
    判断 SQL 是否为安全的单条只读查询。

    `dialect` 是执行器的方言名（`sqlite` / `postgres`）。不传或认不出时按
    SQLite 与 PostgreSQL **各**解析一遍，两种都解析得了、都判只读才放行：
    不知道目标库是哪种，就不能挑对自己有利的那种解读（`$$…$$`、嵌套注释、
    反斜杠转义在两边是两种切法，同一段文字可能是一条或两条语句）。
    两个执行器都会传自己的方言，所以这条路只给不知道目标库的调用方。

    拒绝的情况：
    1. 空语句；
    2. 解析失败；
    3. 一次提交多条语句；
    4. 不是查询语句（写操作、DDL、PRAGMA、ATTACH、SET、COPY、EXPLAIN …）；
    5. 查询里藏着写操作或会话控制（CTE 里的 INSERT/UPDATE/DELETE、
       SELECT INTO、FOR UPDATE …）；
    6. 调用读写文件、执行 SQL 文本、改会话状态的函数；
    7. 附加防线：裸关键字命中禁用词表，或用了 Unicode 转义写法。
    """
    text = (sql or "").strip()
    if not text:
        return SqlGuardResult(False, "SQL 为空。")

    try:
        from sqlglot.dialects.dialect import Dialect
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署事故
        return SqlGuardResult(False, f"缺少 sqlglot，无法做只读校验（不解析即拒绝）：{exc}")

    dialects = _guard_dialects(dialect)
    for name in dialects:
        verdict = _check_one_dialect(text, Dialect.get_or_raise(name))
        if verdict is _EMPTY:
            return SqlGuardResult(False, "SQL 为空。")
        if isinstance(verdict, _ParseFailure):
            suffix = f"（按 {name} 方言解析）" if len(dialects) > 1 else ""
            return SqlGuardResult(False, verdict.reason + suffix)
        if verdict is not None:
            return SqlGuardResult(False, verdict)

    normalized = strip_sql_noise(text).rstrip(";").strip()
    return SqlGuardResult(True, normalized_sql=normalized)


def _guard_dialects(dialect: str) -> Tuple[str, ...]:
    # 与授权解析用同一套方言映射：执行器的 `dialect` 属性直接对得上
    from ai_sql_agent.auth.sql_analysis import _sqlglot_dialect

    read = _sqlglot_dialect(dialect)
    return (read,) if read else _DEFAULT_DIALECTS


@dataclass(frozen=True)
class _ParseFailure:
    reason: str


# 只有注释、只有分号：哪种方言看都是空的
_EMPTY = object()


def _check_one_dialect(text: str, dialect) -> object:
    """
    按一种方言判一遍。

    返回 None 表示放行；字符串是拒绝原因；`_ParseFailure` 表示这种方言解析不了；
    `_EMPTY` 表示解析出来是空的。
    """
    from sqlglot import exp

    try:
        tokens = dialect.tokenize(text)
    except Exception as exc:
        return _ParseFailure(_parse_failure_reason(exc))

    escaped = _unicode_escape(tokens)
    if escaped:
        return (
            f"不支持 Unicode 转义写法（{escaped}）：它能把函数名、表名藏起来，"
            "只读校验看不穿。"
        )

    try:
        statements = dialect.parser().parse(list(tokens), text)
    except Exception as exc:
        return _ParseFailure(_parse_failure_reason(exc))

    # `exp.Semicolon` 是分号后面只剩注释时 sqlglot 留的占位，不是语句
    statements = [
        item
        for item in statements
        if item is not None and not isinstance(item, exp.Semicolon)
    ]
    if not statements:
        return _EMPTY
    if len(statements) > 1:
        return "一次只允许执行一条 SQL 语句。"

    tree = statements[0]
    if not isinstance(tree, _query_roots(exp)):
        # 认不出的语句（`NOTIFY c` 会被解析成「列 NOTIFY 别名 c」）就报它的首个词，
        # 别把 sqlglot 的内部类名报给用户
        known = isinstance(tree, (exp.Command,) + _write_node_types(exp))
        kind = _kind(tree, exp) if known else tokens[0].text.upper()
        return f"只允许执行 SELECT / WITH 查询，这条 SQL 是 {kind} 语句。"

    hits = _forbidden_nodes(tree, exp)
    if hits:
        return f"只读查询里不允许出现 {'、'.join(hits)}。"

    functions = sorted(
        {
            name
            for name in (_function_name(node, exp) for node in tree.find_all(exp.Func))
            if name and is_forbidden_function(name)
        }
    )
    if functions:
        return f"SQL 中包含禁止的函数：{', '.join(functions)}。"

    return _token_backstop(tokens, tree, exp)


def _query_roots(exp) -> tuple:
    """允许做根节点的类型：查询、集合运算、VALUES，以及包了一层括号的它们。"""
    return (exp.Select, exp.SetOperation, exp.Values, exp.Subquery)


def _write_node_types(exp) -> tuple:
    """
    在只读查询里任何位置出现都要拒的节点。

    `getattr` 取：sqlglot 升级时类名有变动不会让这里直接崩，少了的类由
    根节点白名单与附加防线兜住。
    """
    names = (
        "DML",  # Insert / Update / Delete / Merge / Copy
        "DDL",  # Create
        "Drop",
        "Alter",
        "TruncateTable",
        "Command",  # sqlglot 认不出的语句：VACUUM / EXPLAIN / CALL / DO …
        "Into",  # SELECT … INTO new_table
        "Lock",  # FOR UPDATE / FOR SHARE
        "LockingStatement",
        "Pragma",
        "Attach",
        "Detach",
        "Set",
        "SetItem",
        "Transaction",
        "Commit",
        "Rollback",
        "Grant",
        "Revoke",
        "Analyze",
        "Use",
        "Kill",
        "Describe",
        "LoadData",
        "Cache",
        "Uncache",
        "Refresh",
        "Declare",
        "Comment",  # COMMENT ON
        "Export",
        "Put",
        "Get",
        "MultitableInserts",
        "Returning",
        "OnConflict",
        "AlterSession",
    )
    return tuple(cls for cls in (getattr(exp, name, None) for name in names) if cls is not None)


_KIND_NAMES = {
    "Insert": "INSERT",
    "Update": "UPDATE",
    "Delete": "DELETE",
    "Merge": "MERGE",
    "Copy": "COPY",
    "Create": "CREATE",
    "Drop": "DROP",
    "Alter": "ALTER",
    "TruncateTable": "TRUNCATE",
    "Into": "SELECT INTO",
    "Pragma": "PRAGMA",
    "Attach": "ATTACH",
    "Detach": "DETACH",
    "Set": "SET",
    "SetItem": "SET",
    "Transaction": "BEGIN",
    "Commit": "COMMIT",
    "Rollback": "ROLLBACK",
    "Grant": "GRANT",
    "Revoke": "REVOKE",
    "Analyze": "ANALYZE",
    "Use": "USE",
    "Describe": "DESCRIBE",
    "Comment": "COMMENT",
    "Returning": "RETURNING",
    "OnConflict": "ON CONFLICT",
}


def _kind(node, exp) -> str:
    """给拒绝原因用的语句类别名。"""
    if isinstance(node, exp.Command):
        return str(node.this or "未识别").upper()
    if isinstance(node, exp.Lock):
        return "FOR UPDATE" if node.args.get("update") else "FOR SHARE"
    name = type(node).__name__
    return _KIND_NAMES.get(name, name.upper())


def _forbidden_nodes(tree, exp) -> List[str]:
    """整棵树里的写操作 / 会话控制节点，带上位置（WITH 子句、子查询）。"""
    blocked = _write_node_types(exp)
    hits: List[str] = []
    for node in tree.walk():
        if not isinstance(node, blocked):
            continue
        label = _kind(node, exp)
        if node.find_ancestor(exp.CTE) is not None:
            label = f"{label}（在 WITH 子句里）"
        elif node.find_ancestor(exp.Subquery) is not None:
            label = f"{label}（在子查询里）"
        if label not in hits:
            hits.append(label)
    return hits


def _function_name(node, exp) -> str:
    """
    函数节点的名字，小写。

    sqlglot 认识的函数落成具体的类（`Replace`、`Count` …），名字取它的 SQL 名；
    认不出的落成 `Anonymous`，名字就是 SQL 里写的那个——带引号的 `"pg_sleep"(…)`、
    带 schema 的 `pg_catalog.pg_sleep(…)` 都在这里拿到同一个名字。
    """
    anonymous = tuple(
        cls for cls in (exp.Anonymous, getattr(exp, "AnonymousAggFunc", None)) if cls is not None
    )
    if isinstance(node, anonymous):
        return str(node.name or "").lower()
    try:
        return node.sql_name().lower()
    except Exception:
        return ""


def is_forbidden_function(name: str) -> bool:
    return name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES)


def _string_token_types() -> Set:
    from sqlglot.tokens import TokenType

    names = (
        "STRING",
        "NATIONAL_STRING",
        "RAW_STRING",
        "HEREDOC_STRING",
        "BYTE_STRING",
        "BIT_STRING",
        "HEX_STRING",
        "UNICODE_STRING",
    )
    return {getattr(TokenType, name) for name in names if hasattr(TokenType, name)}


def _unicode_escape(tokens: Sequence) -> str:
    """
    PostgreSQL 的 `U&"pg\\005fsleep"` 就是 `pg_sleep`。sqlglot 不解码这种写法，
    把它切成 `U`、`&`、`"pg\\005fsleep"` 三个紧挨着的 token——函数名检查因此
    看不到真名。不去解码（还有 `UESCAPE` 自定义转义符），直接拒。
    """
    from sqlglot.tokens import TokenType

    quoted = _string_token_types() | {TokenType.IDENTIFIER}
    for first, amp, third in zip(tokens, tokens[1:], tokens[2:]):
        if (
            first.text.lower() == "u"
            and amp.token_type == TokenType.AMP
            and third.token_type in quoted
            and first.end + 1 == amp.start
            and amp.end + 1 == third.start
        ):
            return "U&'…' / U&\"…\""
    return ""


def _token_backstop(tokens: Sequence, tree, exp) -> Optional[str]:
    """
    附加防线：在 sqlglot 切出的 token 上再扫一遍关键字和函数调用。

    只看裸词（不看字符串、带引号的标识符；注释本来就不是 token），所以
    `'drop'`、`"Last Update"` 不会被误伤。函数名只在后面紧跟 `(` 时才算
    调用——带引号的函数名也算，`"load_extension"(…)` 一样是调用。

    语法树里不带引号的标识符节点算「认领」：`SELECT name AS copy` 里的
    `copy` 是一个别名，不是 COPY 语句。按**个数**对账而不是按有没有——
    `copy` 在 token 里出现两次、语法树只认领了一个，多出来的那个仍然命中。
    `_ALWAYS_KEYWORDS` 里的词不享受认领。
    """
    from collections import Counter

    from sqlglot.tokens import TokenType

    claimed = Counter(
        str(node.name).lower()
        for node in tree.find_all(exp.Identifier)
        if not node.args.get("quoted")
    )

    literal = _string_token_types()
    keyword_tokens: Counter = Counter()
    call_tokens: Counter = Counter()
    for index, token in enumerate(tokens):
        if token.token_type in literal:
            continue
        word = token.text.lower()
        if token.token_type != TokenType.IDENTIFIER and word in _FORBIDDEN_KEYWORDS:
            keyword_tokens[word] += 1
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if (
            following is not None
            and following.token_type == TokenType.L_PAREN
            and is_forbidden_function(word)
        ):
            call_tokens[word] += 1

    keywords = {
        word
        for word, count in keyword_tokens.items()
        if word in _ALWAYS_KEYWORDS or count > claimed[word]
    }
    functions = {word for word, count in call_tokens.items() if count > claimed[word]}

    if keywords:
        return f"SQL 中包含禁止的关键字：{', '.join(sorted(keywords))}。"
    if functions:
        return f"SQL 中包含禁止的函数：{', '.join(sorted(functions))}。"
    return None


def _parse_failure_reason(exc: Exception) -> str:
    """把 sqlglot 的异常压成一行人能读的原因（去掉终端高亮码与多行上下文）。"""
    detail = ""
    errors = getattr(exc, "errors", None)
    if errors:
        first = errors[0]
        description = _ANSI.sub("", str(first.get("description") or "")).strip()
        line, col = first.get("line"), first.get("col")
        where = f"第 {line} 行第 {col} 列附近：" if line and col else ""
        detail = f"{where}{description}"
    if not detail:
        detail = _ANSI.sub("", str(exc)).strip().splitlines()[0] if str(exc).strip() else ""
    detail = detail or type(exc).__name__
    return f"SQL 无法解析（{detail}），只读校验不放行看不懂的语句。"


def readonly_sqlite_uri(db_path: str) -> Optional[str]:
    """
    生成 SQLite 只读连接 URI。

    文件不存在时返回 None：只读模式不会自动建库，需要调用方给出明确错误。
    """
    from pathlib import Path

    path = Path(db_path)
    if not path.exists():
        return None
    return f"{path.resolve().as_uri()}?mode=ro"
