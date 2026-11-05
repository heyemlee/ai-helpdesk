from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .objects import ColumnSchema, TableRelation, TableSchema


class EmptyDatabaseFile(Exception):
    """
    库文件在，但是 0 字节。

    没人会特意放一个空文件当业务库；它几乎总是「真的库文件没了，之后有谁
    `sqlite3.connect()` 了一下这个路径」留下的。按「库里一张表都没有」报出去是误导。
    """


def _quoted(name: str) -> str:
    """PRAGMA 与抽样语句里的标识符。名字里带双引号的表不该让整个库都读不出来。"""
    return '"' + str(name).replace('"', '""') + '"'


class SQLiteSchemaLoader:
    """SQLite Schema 加载器，用于提取表、字段、主外键和字段样例。"""

    def __init__(
        self,
        db_path: str | Path,
        database_name: str = "main",
        business_meta: Dict[str, Any] | None = None,
        sample_size: int = 5,
    ):
        self.db_path = str(db_path)
        self.database_name = database_name
        self.business_meta = business_meta or {}  # 业务元信息补充，如表描述、字段描述、别名
        self.sample_size = sample_size  # 每个字段最多抽取的样例数量

    def load(self) -> Tuple[Dict[str, TableSchema], List[ColumnSchema], List[TableRelation]]:
        """加载数据库 Schema，返回表信息、字段信息和表关系。"""
        if self.db_path != ":memory:" and not Path(self.db_path).exists():
            # 文件不存在就是「没有表」，不去连：sqlite3.connect() 会顺手建一个空文件，
            # 从那以后谁来看都以为库在、只是空的——库文件丢了这件事就这样被盖住了。
            return {}, [], []

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        try:
            table_names = self._get_table_names(conn)

            tables: Dict[str, TableSchema] = {}
            columns: List[ColumnSchema] = []
            relations: List[TableRelation] = []

            for table_name in table_names:
                table_meta = self.business_meta.get(table_name, {})

                table_info = conn.execute(
                    f"PRAGMA table_info({_quoted(table_name)})"
                ).fetchall()

                foreign_keys = conn.execute(
                    f"PRAGMA foreign_key_list({_quoted(table_name)})"
                ).fetchall()

                primary_keys = [
                    row["name"]
                    for row in table_info
                    if row["pk"] and int(row["pk"]) > 0
                ]

                tables[table_name] = TableSchema(
                    database=self.database_name,
                    table_name=table_name,
                    description=table_meta.get("description", ""),
                    aliases=table_meta.get("aliases", []),
                    primary_keys=primary_keys,
                )

                fk_map: Dict[str, str] = {}

                for fk in foreign_keys:
                    source_col = fk["from"]
                    target_table = fk["table"]
                    target_col = self._foreign_key_target(conn, fk)
                    if not target_col:
                        continue  # 指向的表不在了，或者它没有主键：这条关系说不出连到哪一列

                    fk_map[source_col] = f"{target_table}.{target_col}"

                    relations.append(
                        TableRelation(
                            database=self.database_name,
                            source_table=table_name,
                            source_column=source_col,
                            target_table=target_table,
                            target_column=target_col,
                        )
                    )

                for row in table_info:
                    col_name = row["name"]
                    col_meta = table_meta.get("columns", {}).get(col_name, {})
                    samples = self._get_column_samples(conn, table_name, col_name)

                    columns.append(
                        ColumnSchema(
                            database=self.database_name,
                            table_name=table_name,
                            column_name=col_name,
                            data_type=row["type"] or "UNKNOWN",
                            nullable=not bool(row["notnull"]),
                            description=col_meta.get("description", ""),
                            aliases=col_meta.get("aliases", []),
                            table_description=table_meta.get("description", ""),
                            table_aliases=table_meta.get("aliases", []),
                            samples=samples,
                            is_primary_key=col_name in primary_keys,
                            foreign_key_ref=fk_map.get(col_name),
                        )
                    )

            return tables, columns, relations

        finally:
            conn.close()

    def load_structure(self) -> Dict[str, List[ColumnSchema]]:
        """
        只读表结构：表 → 字段（类型、可空、主外键）。不带业务元数据，不抽样例。

        给数据目录用。与 `load()` 的两点区别都是有意的：

        - 用只读 URI 打开。`sqlite3.connect(path)` 遇到不存在的文件会**新建一个空库**，
          目录就会把「库文件没了」显示成「这个库里一张表都没有」；
        - 不逐字段抽样。目录每次打开都要读一遍，抽样是「字段数」条查询。
        """
        path = Path(self.db_path)
        if not path.exists():
            raise FileNotFoundError(f"数据库文件不存在：{path.name}")
        if path.stat().st_size == 0:
            raise EmptyDatabaseFile(f"数据库文件是空的：{path.name}")

        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            structure: Dict[str, List[ColumnSchema]] = {}
            for table_name in self._get_table_names(conn):
                table_info = conn.execute(
                    f"PRAGMA table_info({_quoted(table_name)})"
                ).fetchall()
                fk_map = {}
                for fk in conn.execute(
                    f"PRAGMA foreign_key_list({_quoted(table_name)})"
                ).fetchall():
                    target_col = self._foreign_key_target(conn, fk)
                    if target_col:
                        fk_map[fk["from"]] = f'{fk["table"]}.{target_col}'
                structure[table_name] = [
                    ColumnSchema(
                        database=self.database_name,
                        table_name=table_name,
                        column_name=row["name"],
                        data_type=row["type"] or "UNKNOWN",
                        nullable=not bool(row["notnull"]),
                        is_primary_key=bool(row["pk"] and int(row["pk"]) > 0),
                        foreign_key_ref=fk_map.get(row["name"]),
                    )
                    for row in table_info
                ]
            return structure
        finally:
            conn.close()

    @staticmethod
    def _foreign_key_target(conn: sqlite3.Connection, fk: sqlite3.Row) -> str:
        """
        这条外键指向对端的哪一列。

        `REFERENCES supplier`（不写列名）是合法写法，意思是指向对端的主键；
        这时 PRAGMA 给的 `to` 是 NULL，得自己去对端查。联合外键按 `seq` 对位。
        """
        if fk["to"]:
            return str(fk["to"])
        try:
            target_info = conn.execute(
                f"PRAGMA table_info({_quoted(fk['table'])})"
            ).fetchall()
        except sqlite3.Error:
            return ""
        keys = sorted(
            (int(row["pk"]), row["name"])
            for row in target_info
            if row["pk"] and int(row["pk"]) > 0
        )
        position = int(fk["seq"] or 0)
        return str(keys[position][1]) if position < len(keys) else ""

    def _get_table_names(self, conn: sqlite3.Connection) -> List[str]:
        """获取业务表名，过滤 SQLite 系统表。"""
        rows = conn.execute(
            '''
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            '''
        ).fetchall()

        return [row["name"] for row in rows]

    def _get_column_samples(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
    ) -> List[str]:
        """抽取字段非空样例值，用于增强字段语义。"""
        try:
            sql = (
                f"SELECT DISTINCT {_quoted(column_name)} AS value "
                f"FROM {_quoted(table_name)} "
                f"WHERE {_quoted(column_name)} IS NOT NULL "
                f"LIMIT ?"
            )

            rows = conn.execute(sql, (self.sample_size,)).fetchall()
            return [str(row["value"]) for row in rows if row["value"] is not None]

        except Exception:
            return []