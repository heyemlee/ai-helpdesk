"""Generate bilingual semantic metadata for the approved simplified schema."""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

import yaml

try:
    from .schema import DATABASES
except ImportError:  # Direct script execution.
    from schema import DATABASES


SHARED = {
    "cross_database_links": [
        {"left": "erp_db.orders.order_no", "right": f"{db}.{table}.order_no",
         "meaning": "同一订单号，按订单号关联；一个订单可有多条配送、售后或补件记录，汇总前注意重复行。",
         "confidence": "naming_convention"}
        for db, table in (("prod_db", "work_order"), ("svc_db", "delivery"),
                          ("svc_db", "service_ticket"), ("svc_db", "replacement_part"))
    ] + [
        {"left": "wms_db.material.material_name", "right": "svc_db.replacement_part.part_name",
         "meaning": "补件名称对应仓储物料名称及规格，不使用物料编号。", "confidence": "naming_convention"},
    ],
    "metric_disambiguation": [
        {"term": "合同金额 / 订单金额", "correct": "erp_db.orders.contract_amount",
         "wrong": "erp_db.order_item.subtotal（产品明细小计，不能代替谈定合同总额）"},
        {"term": "交付日期 / 交期", "correct": "erp_db.orders.delivery_date",
         "wrong": "svc_db.delivery.delivered_date（这是实际送达日期）"},
        {"term": "实际送达 / 何时送到", "correct": "svc_db.delivery.delivered_date",
         "wrong": "erp_db.orders.delivery_date（这是约定交付日期）"},
        {"term": "可用库存", "correct": "wms_db.material.on_hand_qty",
         "wrong": "只读取现有库存而不减去 allocated_qty，会高估可用数量"},
    ],
    "metric_definitions": {
        "订单金额": "按 orders.contract_amount 统计不含税美元合同金额。统计有效订单时排除 order_status='已取消'。不把订单明细行金额相加后当作合同额。",
        "小计": "order_item.quantity × order_item.unit_price，保留两位小数，数据库自动计算。",
        "可用库存": "material.on_hand_qty − material.allocated_qty；物料名称含规格，按名称区分材料；不同计量单位的数量不能直接相加。",
        "低于安全库存": "material.on_hand_qty < material.safety_stock。",
        "到货状态": "receipt.received_qty=0 为未到货；大于零且小于 purchased_qty 为部分到货；等于 purchased_qty 为全部到货。此状态通过数量计算，不是存储字段。",
        "未到货数量": "receipt.purchased_qty − receipt.received_qty，按物料名称和其计量单位统计。",
        "在制": "work_order.production_status='生产中'，包含暂停但未完工的订单；暂停原因记录在 production_notes。",
        "未解决售后": "service_ticket.service_status IN ('待处理','处理中')。",
        "未解决补件": "replacement_part.part_status IN ('待处理','处理中')；有 shipped_date 仅表示已发出，不代表已解决。",
        "逾期交付": "先从 ERP 查询 delivery_date 已过且未取消的订单，再按 order_no 从配送库检查是否尚未送达。约定日期为空时不能判断逾期。",
        "数据范围": "只有九张业务表，没有客户独立表、应收发票、回款、变更单、工序流水或质检表。不能据此计算未付余额、良率、工序耗时或供应商准时率；到货记录没有预计日期。手机号、销售人员、供应商联系人地址和配送人员电话缺失时为空，不可推测。",
    },
}


def build_semantic(db_dir: Path, out_dir: Path) -> list[Path]:
    db_dir, out_dir = Path(db_dir), Path(out_dir)
    documents = {}
    for db, (filename, label, label_en, tables) in DATABASES.items():
        path = db_dir / filename
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if actual != set(tables):
                raise ValueError(f"Table mismatch in {db}: expected {sorted(tables)}, got {sorted(actual)}")
            doc = {
                "name": db,
                "label_zh": label,
                "label_en": label_en,
                "description": f"精简{label}数据库：" + "、".join(t.label for t in tables.values()) + "。",
                "aliases": [label, label_en, db.split("_")[0], "MES" if db == "prod_db" else db.split("_")[0].upper()],
                "connection": {"dialect": "sqlite", "db_path": str(path)},
                "tables": {},
            }
            for name, table in tables.items():
                info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
                if [row[1] for row in info] != [c.name for c in table.columns]:
                    raise ValueError(f"Column mismatch in {db}.{name}")
                entry = {
                    "label_zh": table.label, "label_en": table.label_en,
                    "description": table.description,
                    "aliases": [table.label, table.label + "表", table.label_en, name.replace("_", " ")],
                    "columns": {},
                }
                primary = [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
                if primary:
                    entry["primary_key"] = primary
                for column in table.columns:
                    values_text = "；选项：" + "、".join(column.values) if column.values else ""
                    description = column.label + values_text + "。" + column.usage
                    aliases = [column.label, column.label_en, column.name.replace("_", " ")]
                    samples = [str(r[0]) for r in conn.execute(
                        f'SELECT DISTINCT "{column.name}" FROM "{name}" WHERE "{column.name}" IS NOT NULL LIMIT 5')]
                    usage = column.usage or f"查询和展示{column.label}。"
                    if column.role == "time":
                        usage += " 日期为 YYYY-MM-DD 文本；本地时间相关查询使用 DATE('now','localtime')。"
                    meta = {
                        "label_zh": column.label, "label_en": column.label_en,
                        "description": description, "aliases": aliases,
                        "semantic_role": column.role, "business_usage": usage,
                        "keyword_text": " ".join([name, column.name, *aliases, *column.values]),
                        "vector_text": f"{table.label}的{column.label}：{description}",
                        "rerank_text": f"{db}.{name}.{column.name}：{description} 用途：{usage}",
                    }
                    if samples:
                        meta["samples"] = samples
                    if column.values:
                        meta["value_range"] = " / ".join(column.values)
                    if column.unit:
                        meta["unit"] = column.unit
                    entry["columns"][column.name] = meta
                doc["tables"][name] = entry
            documents[db + ".yaml"] = doc
    documents["_shared.yaml"] = SHARED
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, doc in documents.items():
        path = out_dir / filename
        path.write_text("# Generated by tools/simple_erp/semantic.py; edit schema.py instead.\n"
                        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("runtime_data/simple"))
    parser.add_argument("--out", type=Path, default=Path("tools/simple_erp/semantic"))
    args = parser.parse_args()
    for path in build_semantic(args.db, args.out):
        print(path)


if __name__ == "__main__":
    main()
