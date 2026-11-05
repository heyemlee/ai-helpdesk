"""Convert the existing simulated records to the approved simplified databases.

The source databases are opened read-only. All four outputs are staged and checked
before publishing; an existing output is backed up before replacement.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

try:
    from .schema import DATABASES, create_schema
except ImportError:  # Direct script execution.
    from schema import DATABASES, create_schema


STAGES = {"cut": "裁切", "edgeband": "封边", "drill": "钻孔", "assembly": "组装",
          "finish": "组装", "qc": "包装", "pack": "包装", "done": "包装"}
PRODUCTION = {"scheduled": "待生产", "in_progress": "生产中", "on_hold": "生产中", "complete": "已完工"}
DELIVERY = {"scheduled": "待配送", "failed": "待配送", "rescheduled": "待配送",
            "in_transit": "配送中", "delivered": "已送达"}
METHODS = {"own_truck": "自送", "ltl_freight": "物流", "customer_pickup": "客户自提"}
ISSUES = {"missing_part": "缺件", "damage": "破损", "defect": "质量问题", "install_issue": "安装问题", "wrong_item": "发错货"}
TICKETS = {"open": "待处理", "in_progress": "处理中", "waiting_parts": "处理中",
           "resolved": "已解决", "closed": "已解决"}
PARTS = {"requested": "待处理", "in_production": "处理中", "shipped": "处理中", "received": "已解决"}
UNITS = {"each": "件", "sheet": "张", "lf": "英尺", "sqft": "平方英尺"}
CATEGORIES = {"door": "门板", "box": "箱体", "hardware": "五金", "countertop": "台面", "finish": "涂料"}


def read_source(source: Path) -> dict[str, dict[str, list[dict]]]:
    data = {}
    for db_name, (filename, *_rest) in DATABASES.items():
        path = source / filename
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Source database missing or empty: {path}")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            data[db_name] = {
                table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table.replace(chr(34), chr(34)*2)}" ORDER BY rowid')]
                for table in tables
            }
    return data


def _notes(*parts: str | None) -> str | None:
    return "；".join(str(part) for part in parts if part) or None


def transform(data: dict) -> dict[str, dict[str, list[tuple]]]:
    erp, prod, wms, svc = (data[name] for name in DATABASES)
    customers = {row["customer_id"]: row for row in erp["customer"]}
    work = {row["job_no"]: row for row in prod["work_order"]}
    deliveries = defaultdict(list)
    for row in svc["delivery"]:
        deliveries[row["job_no"]].append(row)
    tickets = {row["ticket_no"]: row for row in svc["service_ticket"]}
    suppliers = {row["supplier_id"]: row["supplier_name"] for row in wms["supplier"]}
    if len(set(suppliers.values())) != len(suppliers):
        raise ValueError("Supplier names must be unique before removing supplier identifiers.")

    materials = defaultdict(list)
    material_names = {}
    for row in wms["material"]:
        materials[row["description"]].append(row)
        material_names[row["sku"]] = row["description"]
    inventory = {row["sku"]: row for row in wms["inventory"]}
    material_rows = []
    for name, rows in materials.items():
        units = {row["uom"] for row in rows}
        if len(units) != 1:
            raise ValueError(f"Same-name material has incompatible units: {name}")
        stock = [inventory[row["sku"]] for row in rows]
        material_rows.append((name, UNITS[rows[0]["uom"]],
                              sum(row["on_hand_qty"] for row in stock),
                              sum(row["allocated_qty"] for row in stock),
                              sum(row["safety_stock"] for row in rows),
                              "、".join(sorted({row["location"] for row in stock})),
                              f"合并 {len(rows)} 条同名、同单位的物料库存。" if len(rows) > 1 else None))

    order_rows = []
    for row in erp["job"]:
        delivery_rows = deliveries[row["job_no"]]
        production = work.get(row["job_no"])
        if row["job_status"] == "cancelled":
            state = "已取消"
        elif any(d["delivery_status"] == "delivered" and d["delivered_date"] for d in delivery_rows):
            state = "已交付"
        elif any(d["delivery_status"] == "in_transit" for d in delivery_rows):
            state = "配送中"
        elif production and production["wo_status"] == "complete":
            state = "待发货"
        elif production and production["wo_status"] in ("in_progress", "on_hold"):
            state = "生产中"
        else:
            state = "已签单"
        planned = sorted(d["scheduled_date"] for d in delivery_rows if d["scheduled_date"])
        order_rows.append((row["job_no"], customers[row["customer_id"]]["customer_name"],
                           None, row["contract_amount"], row["sold_date"], planned[0] if planned else None,
                           state, f"{row['address']}, {row['city']}, {row['state_norm']}", None, None))

    result = {
        "erp_db": {
            "orders": order_rows,
            "order_item": [(r["job_no"], r["description"], r["sku"], r["qty"], r["unit_price"],
                            round(r["qty"] * r["unit_price"], 2)) for r in erp["job_item"]],
        },
        "prod_db": {
            "work_order": [(r["job_no"], STAGES[r["current_stage"]], PRODUCTION[r["wo_status"]],
                            int(r["priority"] == "rush"),
                            _notes(r["hold_reason"], {"finish": "涂装阶段", "qc": "质检阶段"}.get(r["current_stage"])))
                           for r in prod["work_order"]],
        },
        "wms_db": {
            "material": material_rows,
            "supplier": [(r["supplier_name"], None, None, None,
                          "供货范围：" + CATEGORIES[r["category"]]) for r in wms["supplier"]],
            "receipt": [(material_names[r["sku"]], suppliers[r["supplier_id"]], r["qty"],
                         r["qty"] if r["received_date"] else 0, r["received_date"], None)
                        for r in wms["receipt"]],
        },
        "svc_db": {
            "delivery": [(r["job_no"], METHODS[r["carrier"]], DELIVERY[r["delivery_status"]],
                          None, None, r["delivered_date"],
                          _notes(r["notes"], {"failed": "配送失败，待重新配送", "rescheduled": "已改约配送"}.get(r["delivery_status"])))
                         for r in svc["delivery"]],
            "service_ticket": [(r["job_no"], ISSUES[r["issue_type"]], r["description"], r["reported_date"],
                                TICKETS[r["ticket_status"]], r["resolved_date"],
                                _notes("报修人：" + r["reported_by"], "等待补件" if r["ticket_status"] == "waiting_parts" else None))
                               for r in svc["service_ticket"]],
            "replacement_part": [(tickets[r["ticket_no"]]["job_no"], material_names[r["sku"]], r["qty"],
                                  r["requested_date"], PARTS[r["request_status"]], r["shipped_date"],
                                  _notes("补件原因：" + ISSUES[tickets[r["ticket_no"]]["issue_type"]],
                                         "已发出，等待收件" if r["request_status"] == "shipped" else None))
                                 for r in svc["replacement_part"]],
        },
    }
    order_numbers = {row[0] for row in order_rows}
    for db, table in (("prod_db", "work_order"), ("svc_db", "delivery"),
                      ("svc_db", "service_ticket"), ("svc_db", "replacement_part")):
        if any(row[0] not in order_numbers for row in result[db][table]):
            raise ValueError(f"Order reference missing: {db}.{table}")
    return result


def build(source: Path, out: Path) -> dict[str, int]:
    source, out = Path(source), Path(out)
    if source.resolve() == out.resolve():
        raise ValueError("Output must differ from the source; original records are preserved.")
    rows = transform(read_source(source))
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = {}
    with tempfile.TemporaryDirectory(prefix="simple-erp-", dir=out.parent) as staging:
        for db, (filename, _label, _en, tables) in DATABASES.items():
            path = Path(staging) / filename
            with closing(sqlite3.connect(path)) as conn:
                create_schema(conn, tables)
                for name, table in tables.items():
                    placeholders = ",".join("?" for _ in table.columns)
                    conn.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows[db][name])
                    counts[f"{db}.{name}"] = len(rows[db][name])
                conn.commit()
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError(f"Database integrity check failed: {db}")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError(f"Foreign key check failed: {db}")
        out.mkdir(parents=True, exist_ok=True)
        existing = [out / values[0] for values in DATABASES.values() if (out / values[0]).exists()]
        if existing:
            stamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y%m%d-%H%M%S-%f")
            backup = out.parent / "backups" / f"simple-erp-{stamp}"
            backup.mkdir(parents=True)
            for path in existing:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(backup / path.name)) as dst:
                    src.backup(dst)
        for filename, *_ in DATABASES.values():
            shutil.move(str(Path(staging) / filename), str(out / filename))
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("runtime_data/sim"))
    parser.add_argument("--out", type=Path, default=Path("runtime_data/simple"))
    parser.add_argument("--semantic", type=Path, default=Path("tools/simple_erp/semantic"))
    args = parser.parse_args()
    counts = build(args.source, args.out)
    try:
        from .semantic import build_semantic
    except ImportError:
        from semantic import build_semantic
    build_semantic(args.out, args.semantic)
    for table, count in counts.items():
        print(f"{table}: {count:,}")
    print(f"Total: {sum(counts.values()):,} records; databases: {args.out}; semantics: {args.semantic}")


if __name__ == "__main__":
    main()
