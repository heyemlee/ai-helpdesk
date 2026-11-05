from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

SHARED_FILE_STEM = "_shared"
"""目录里以下划线开头的文件是跨库共享的语义（关联键、口径消歧、指标定义）。"""

CONNECTION_KEYS = ("dialect", "db_path", "dsn", "dsn_env")

DISPLAY_KEYS = (
    "label", "label_en", "label_zh",
    "description_en", "description_zh",
    "business_usage_en", "business_usage_zh",
)
"""
给人看的名字与说明，库、表、字段三层都认这几个键，全部可选：

- `label`：业务名称，不分语言；`label_en` / `label_zh` 是分语言的写法；
- `description_en` / `description_zh`：`description` 的对应语言版本；
- `business_usage_en` / `business_usage_zh`：字段的 `business_usage` 的对应语言版本。

**它们不进检索与生成 Prompt**——模型读的仍然是 `description` 与 `aliases`。
这几个键只给数据目录（`ai_sql_agent.catalog`）用：模型需要的是能召回字段的
口语别名，人需要的是一个不带下划线的名字，两者不是同一份文案。
"""


def display_fields(meta: Any) -> Dict[str, str]:
    """从一段元数据里取出展示用的键；没写的不出现。"""
    if not isinstance(meta, dict):
        return {}
    return {
        key: str(meta[key]).strip()
        for key in DISPLAY_KEYS
        if isinstance(meta.get(key), (str, int, float)) and str(meta[key]).strip()
    }


@dataclass
class DatabaseSemantics:
    """
    一个业务库的语义元数据：它是什么系统、怎么连、每张表每个字段是什么意思。

    `tables` 沿用 ai_sql_agent.pipeline.demo_data 里 business_meta 的结构
    （表 → description / aliases / columns → 字段 → description / aliases /
    semantic_role / business_usage / value_range / samples / keyword_text ...），
    检索、术语表、规划校验都直接读这份字典，不做二次转换——
    元数据文件里写什么，模型就看到什么。
    """

    name: str
    description: str = ""
    aliases: List[str] = field(default_factory=list)

    dialect: str = "sqlite"
    db_path: str = ""
    """SQLite 库文件路径；dialect 为 postgres 时忽略。"""

    dsn: str = ""
    """PostgreSQL 连接串。生产上别写进文件，用 dsn_env 指向环境变量。"""

    dsn_env: str = ""
    """存放连接串的环境变量名，例如 ERP_DSN。连接串含密码，文件要进 git，密码不能。"""

    tables: Dict[str, dict] = field(default_factory=dict)

    display: Dict[str, str] = field(default_factory=dict)
    """库这一层的展示用键（见 `DISPLAY_KEYS`），例如 `label_en: Orders & Purchasing`。"""

    def resolved_dsn(self) -> str:
        if self.dsn:
            return self.dsn
        if self.dsn_env:
            return os.environ.get(self.dsn_env, "")
        return ""

    def has_connection(self) -> bool:
        if self.dialect == "postgres":
            return bool(self.resolved_dsn())
        return bool(self.db_path)

    def catalog_entry(self) -> dict:
        """库级检索（Tier 1）用的目录条目。"""
        return {
            "name": self.name,
            "description": self.description,
            "aliases": list(self.aliases),
        }

    def to_dict(self) -> dict:
        """文件表示：连接信息单独放在 connection 下，业务人员改表字段时不用碰它。"""
        connection: Dict[str, Any] = {"dialect": self.dialect}
        if self.db_path:
            connection["db_path"] = str(self.db_path)
        if self.dsn:
            connection["dsn"] = self.dsn
        if self.dsn_env:
            connection["dsn_env"] = self.dsn_env
        return {
            "name": self.name,
            **self.display,
            "description": self.description,
            "aliases": list(self.aliases),
            "connection": connection,
            "tables": self.tables,
        }

    @classmethod
    def from_dict(cls, payload: dict, fallback_name: str = "") -> "DatabaseSemantics":
        name = str(payload.get("name") or fallback_name).strip()
        if not name:
            raise ValueError("数据库元数据缺少 name")
        connection = payload.get("connection") or {}
        if not isinstance(connection, dict):
            raise ValueError(f"{name}: connection 必须是键值对")
        # 也接受把连接字段直接写在顶层，少一层缩进
        for key in CONNECTION_KEYS:
            if key in payload and key not in connection:
                connection[key] = payload[key]
        tables = payload.get("tables") or {}
        if not isinstance(tables, dict):
            raise ValueError(f"{name}: tables 必须是「表名 → 说明」的键值对")
        aliases = payload.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        return cls(
            name=name,
            description=str(payload.get("description") or "").strip(),
            aliases=[str(item) for item in aliases],
            dialect=str(connection.get("dialect") or "sqlite"),
            db_path=str(connection.get("db_path") or ""),
            dsn=str(connection.get("dsn") or ""),
            dsn_env=str(connection.get("dsn_env") or ""),
            tables=tables,
            display=display_fields(payload),
        )


@dataclass
class SemanticLayer:
    """
    整个系统的语义层：所有库的元数据 + 跨库共享的口径。

    这是模型认识业务的唯一入口。检索靠它的别名与描述召回字段，
    规划靠它的跨库关联键拆步，问答靠它的指标定义解释口径。
    元数据缺了哪一块，对应那一类问题就答不对——这是数据劳动，不是模型能力。
    """

    databases: Dict[str, DatabaseSemantics] = field(default_factory=dict)
    cross_database_links: List[dict] = field(default_factory=list)
    metric_disambiguation: List[dict] = field(default_factory=list)
    metric_definitions: Dict[str, str] = field(default_factory=dict)
    """业务指标定义：「超预算」「良率」这类词到底怎么算，对话问答链路直接引用。"""

    metric_definitions_en: Dict[str, Dict[str, str]] = field(default_factory=dict)
    """
    指标定义的英文版，只给数据目录展示用：`{指标名: {"term": 英文名, "definition": 英文定义}}`。

    键是 `metric_definitions` 里的那个指标名。**模型读的仍然是 `metric_definitions`**——
    口径只有一份；这里是同一份口径给另一种语言的读者看的写法，不参与问答。
    """

    source: str = "demo"
    """来源：demo 表示内置演示元数据，否则是加载的目录路径。"""

    @property
    def is_demo(self) -> bool:
        return self.source == "demo"

    def database_names(self) -> List[str]:
        return list(self.databases.keys())

    def business_metas(self) -> Dict[str, dict]:
        return {name: database.tables for name, database in self.databases.items()}

    def catalog(self) -> List[dict]:
        return [database.catalog_entry() for database in self.databases.values()]

    def shared_to_dict(self) -> dict:
        payload = {
            "cross_database_links": list(self.cross_database_links),
            "metric_disambiguation": list(self.metric_disambiguation),
            "metric_definitions": dict(self.metric_definitions),
        }
        if self.metric_definitions_en:
            # 没写英文版时不输出这个键：内置演示元数据导出来的文件保持原样
            payload["metric_definitions_en"] = {
                term: dict(entry) for term, entry in self.metric_definitions_en.items()
            }
        return payload

    # ------------------------------------------------------------------
    # 契约 §4.6.5 / §4.6.6：单位与来源记录
    #
    # 币种是**数据的属性**，不是调用方的属性。服务端不下发的时候，调用方只能在
    # 自己那边配一个全局环境变量顶上（`SQL_AGENT_CURRENCY`），而一个 agent 接多个
    # 库、或一个库里同时有 USD 与 CNY 列时，那个全局常量必然给出错误答案——
    # 而且是静默的，报出去的金额看起来完全正常。这已经出过一次业务事故。
    #
    # 单位的自然存放位置就是这里：语义层本来就是「这个字段是什么意思」的唯一出处。
    # ------------------------------------------------------------------

    def column_unit(self, database: str, table: str, column: str) -> str:
        """
        字段单位标识（`USD` / `EA` / `day` …）。没有声明就返回空串。

        **返回空串时调用方不得擅自补单位**（契约 §4.6.5）——「不知道」和「没有单位」
        是两回事，前者必须原样呈现数字并说明单位未知。
        """
        meta = self._column_meta(database, table, column)
        return str(meta.get("unit") or "").strip()

    def primary_key_columns(self, database: str, table: str) -> List[str]:
        """
        主键列（契约 §4.6.6 的 `key_columns`）。没有声明就返回空列表。

        用来把「客服说的这句话」追回到**哪几条业务记录**。今天能追到的最细粒度
        是一次查询（trace_id），追不到一条记录——出了纠纷只能拿 final_sql 回去
        重跑，而重跑得到的是今天的数据，不是当时的数据。
        """
        table_meta = self._table_meta(database, table)
        keys = table_meta.get("primary_key") or []
        if isinstance(keys, str):
            keys = [keys]
        return [str(item) for item in keys if str(item).strip()]

    def _table_meta(self, database: str, table: str) -> dict:
        semantics = self.databases.get(database)
        if semantics is None:
            return {}
        meta = semantics.tables.get(table)
        return meta if isinstance(meta, dict) else {}

    def _column_meta(self, database: str, table: str, column: str) -> dict:
        columns = self._table_meta(database, table).get("columns") or {}
        if not isinstance(columns, dict):
            return {}
        meta = columns.get(column)
        return meta if isinstance(meta, dict) else {}

    def merge_shared(self, payload: dict) -> None:
        """把一个共享文件的内容合并进来；多个共享文件按文件名顺序叠加。"""
        links = payload.get("cross_database_links") or []
        disambiguation = payload.get("metric_disambiguation") or []
        definitions = payload.get("metric_definitions") or {}
        if not isinstance(links, list) or not isinstance(disambiguation, list):
            raise ValueError("cross_database_links / metric_disambiguation 必须是列表")
        if not isinstance(definitions, dict):
            raise ValueError("metric_definitions 必须是「指标名 → 定义」的键值对")
        self.cross_database_links.extend(dict(item) for item in links)
        self.metric_disambiguation.extend(dict(item) for item in disambiguation)
        self.metric_definitions.update(
            {str(key): str(value) for key, value in definitions.items()}
        )

        english = payload.get("metric_definitions_en") or {}
        if not isinstance(english, dict):
            raise ValueError("metric_definitions_en 必须是「指标名 → {term, definition}」的键值对")
        for term, entry in english.items():
            if isinstance(entry, str):
                entry = {"definition": entry}
            if not isinstance(entry, dict):
                raise ValueError(f"metric_definitions_en.{term} 必须是 {{term, definition}}")
            cleaned = {
                key: str(entry[key]).strip()
                for key in ("term", "definition")
                if str(entry.get(key) or "").strip()
            }
            if cleaned:
                self.metric_definitions_en[str(term)] = cleaned
