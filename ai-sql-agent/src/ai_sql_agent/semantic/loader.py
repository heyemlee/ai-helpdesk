from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .objects import SHARED_FILE_STEM, DatabaseSemantics, SemanticLayer

SUPPORTED_SUFFIXES = (".yaml", ".yml", ".json")

_YAML_HEADER = """# AI SQL Agent 语义元数据。
# 这份文件决定模型「认不认识」这个库：别名决定口语问法能不能召回字段，
# description 决定模型理解字段含义，semantic_role 决定它当筛选、指标还是关联键用。
# 改完跑 `python -m ai_sql_agent.semantic validate <目录>` 比对真实库结构。
"""

_SHARED_HEADER = """# 跨库共享的语义：关联键、同名指标的口径消歧、业务指标定义。
# cross_database_links：不同库之间按命名约定对应的键（没有外键时模型只能靠这里拆步）。
# metric_disambiguation：「采购金额」这类多个库都有的说法，正确字段是哪个、容易错到哪个。
# metric_definitions：「超预算」「良率」这类业务词的定义，对话问答直接引用。
"""


def load_semantic_dir(path: str | Path) -> SemanticLayer:
    """
    读取一个语义元数据目录。

    目录约定：每个库一个文件（erp_db.yaml / erp_db.json），文件名或文件里的
    name 就是库名；以下划线开头的文件（_shared.yaml）是跨库共享的语义，
    可以有多个，按文件名顺序合并。YAML 与 JSON 可以混用。
    """
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"语义元数据目录不存在：{directory}")

    files = sorted(
        item
        for item in directory.iterdir()
        if item.is_file()
        and item.suffix.lower() in SUPPORTED_SUFFIXES
        and not item.name.startswith(".")
    )
    if not files:
        raise ValueError(
            f"目录 {directory} 里没有 .yaml / .json 元数据文件。"
            "可以先用 `python -m ai_sql_agent.semantic export <目录>` 导出一份模板。"
        )

    layer = SemanticLayer(source=str(directory))
    for file in files:
        payload = _read_file(file)
        if file.stem.startswith("_"):
            try:
                layer.merge_shared(payload)
            except ValueError as exc:
                raise ValueError(f"{file.name}: {exc}") from exc
            continue

        try:
            database = DatabaseSemantics.from_dict(payload, fallback_name=file.stem)
        except ValueError as exc:
            raise ValueError(f"{file.name}: {exc}") from exc
        if database.name in layer.databases:
            raise ValueError(f"{file.name}: 数据库 {database.name} 重复定义")
        layer.databases[database.name] = database

    if not layer.databases:
        raise ValueError(
            f"目录 {directory} 里只有共享文件，没有任何数据库文件（不以下划线开头的文件）。"
        )
    return layer


def _read_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text) if text.strip() else {}
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise RuntimeError(
                f"读取 {path.name} 需要 pyyaml：pip install pyyaml，或改用 .json"
            ) from exc
        data = yaml.safe_load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: 顶层必须是键值对")
    return data


def write_semantic_dir(
    layer: SemanticLayer,
    path: str | Path,
    fmt: str = "yaml",
    overwrite: bool = False,
) -> List[Path]:
    """
    把一个语义层写成目录（每库一个文件 + _shared 文件）。

    主要用途是导出内置 Demo 元数据当模板：业务人员照着改，比对着空白格式文档写快得多。
    """
    if fmt not in ("yaml", "json"):
        raise ValueError("fmt 只能是 yaml 或 json")
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for name, database in layer.databases.items():
        target = directory / f"{name}.{fmt}"
        _write(target, database.to_dict(), fmt, overwrite, header=_YAML_HEADER)
        written.append(target)

    shared = directory / f"{SHARED_FILE_STEM}.{fmt}"
    _write(shared, layer.shared_to_dict(), fmt, overwrite, header=_SHARED_HEADER)
    written.append(shared)
    return written


def _write(target: Path, payload: dict, fmt: str, overwrite: bool, header: str) -> None:
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} 已存在；加 --overwrite 才会覆盖")
    if fmt == "json":
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return
    import yaml

    body = yaml.safe_dump(
        payload, allow_unicode=True, sort_keys=False, width=100, default_flow_style=False
    )
    target.write_text(header + "\n" + body, encoding="utf-8")


def demo_semantic_layer() -> SemanticLayer:
    """
    内置的四库演示元数据，包成语义层。

    Demo 元数据继续留在 ai_sql_agent.pipeline.demo_data 里当单一事实来源
    （测试、离线演示都靠它）；这里只是换一个入口，让 Demo 与真实库走同一条路径。
    """
    from ai_sql_agent.pipeline import demo_data  # 延迟导入，避免与 ai_sql_agent.pipeline 循环依赖

    catalog = {item["name"]: item for item in demo_data.get_database_catalog()}
    metas = {
        "erp_db": demo_data.get_erp_business_meta,
        "pm_db": demo_data.get_pm_business_meta,
        "mes_db": demo_data.get_mes_business_meta,
        "wms_db": demo_data.get_wms_business_meta,
    }
    paths = {
        "erp_db": "runtime_data/erp_demo.db",
        "pm_db": "runtime_data/pm_demo.db",
        "mes_db": "runtime_data/mes_demo.db",
        "wms_db": "runtime_data/wms_demo.db",
    }

    layer = SemanticLayer(source="demo")
    for name, builder in metas.items():
        entry = catalog.get(name, {})
        layer.databases[name] = DatabaseSemantics(
            name=name,
            description=entry.get("description", ""),
            aliases=list(entry.get("aliases", [])),
            dialect="sqlite",
            db_path=paths[name],
            tables=builder(),
        )
    layer.cross_database_links = list(demo_data.get_cross_database_links())
    layer.metric_disambiguation = list(demo_data.get_metric_disambiguation())
    layer.metric_definitions = dict(demo_data.get_metric_definitions())
    return layer
