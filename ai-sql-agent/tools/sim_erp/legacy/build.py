"""
把 sim_erp 的业务事实原样搬进遗留命名的 schema。

**同一批数据，换一身皮。** 不重新生成随机数据，是为了让两边可比：同一个问题在
sim_erp（业务白话字段名）和 legacy（T_SAL_ORDER / FBILLNO / 单字母状态码）上问，
答案应该一模一样。答不出来，差距就精确地落在命名这一层——那正是接真 ERP 时
语义层要填的东西，也是 sim_erp 那份数据没能预演的。

三处刻意的"真 ERP 味道"：
  · 日期存成 8 位整数 20260903，不是 '2026-09-03'。日期比较要先转格式
  · 状态存单字母：订单 A/B/C/D/E/F/Z，发票 O/P/S/V，客户禁用位 A/B
  · 金额列全叫 FAMOUNT——订单头是不含税合同额、明细行是行金额、应收是开票额，
    三个 FAMOUNT 口径不同，光看字段名分不出来
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

# 业务状态 → 遗留单字母代码。真 ERP 里这张表通常只存在于某个人的脑子里。
ORDER_STATUS = {"sold": "A", "released": "B", "in_production": "C",
                "shipped": "D", "installed": "E", "closed": "F", "cancelled": "Z"}
INVOICE_STATUS = {"open": "O", "partial": "P", "paid": "S", "void": "V"}
CUST_TYPE = {"production_builder": "01", "general_contractor": "02",
             "dealer": "03", "retail": "04"}
PAY_COND = {"net_30": "M30", "net_45": "M45", "net_60": "M60", "deposit_50": "D50"}


def _d(iso: str | None) -> int | None:
    """'2026-09-03' → 20260903。真 ERP 里日期常年是整数。"""
    return int(iso.replace("-", "")) if iso else None


def build(src: Path, out: Path) -> dict[str, int]:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    dst = sqlite3.connect(str(out))
    dst.executescript((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
    s = sqlite3.connect(str(src))
    s.row_factory = sqlite3.Row

    dst.executemany("INSERT INTO T_BD_CUSTOMER VALUES (?,?,?,?,?,?)", [
        (r["customer_id"], r["customer_name"], CUST_TYPE[r["customer_type"]],
         PAY_COND[r["payment_terms"]], r["credit_limit"], "A" if r["is_active"] else "B")
        for r in s.execute("SELECT * FROM customer")])

    dst.executemany("INSERT INTO T_SAL_ORDER VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (r["job_no"], r["customer_id"], r["subdivision"], r["lot_no"], r["plan_name"],
         r["address"], r["city"], r["state"], r["state_norm"], r["job_type"],
         r["contract_amount"], _d(r["sold_date"]), _d(r["target_install_date"]),
         ORDER_STATUS[r["job_status"]])
        for r in s.execute("SELECT * FROM job")])

    dst.executemany("INSERT INTO T_SAL_ORDERENTRY VALUES (?,?,?,?,?,?,?,?,?)", [
        (r["item_id"], r["job_no"], r["line_no"], r["category"], r["sku"],
         r["description"], r["qty"], r["unit_price"], r["extended_price"])
        for r in s.execute("SELECT * FROM job_item")])

    dst.executemany("INSERT INTO T_SAL_CHGORDER VALUES (?,?,?,?,?,?,?)", [
        (r["change_order_no"], r["job_no"], r["reason"], r["amount_delta"],
         _d(r["requested_date"]), _d(r["approved_date"]),
         {"pending": "A", "approved": "C", "rejected": "R"}[r["co_status"]])
        for r in s.execute("SELECT * FROM change_order")])

    dst.executemany("INSERT INTO T_AR_RECEIVABLE VALUES (?,?,?,?,?,?,?)", [
        (r["invoice_no"], r["job_no"], r["invoice_type"], _d(r["invoice_date"]),
         r["amount"], _d(r["due_date"]), INVOICE_STATUS[r["invoice_status"]])
        for r in s.execute("SELECT * FROM invoice")])

    dst.executemany("INSERT INTO T_AR_RECEIVEBILL VALUES (?,?,?,?,?)", [
        (r["payment_id"], r["invoice_no"], r["amount"], _d(r["received_date"]), r["method"])
        for r in s.execute("SELECT * FROM payment_received")])

    dst.commit()
    counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    s.close(); dst.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="把 sim_erp 数据搬进遗留命名的 schema。")
    ap.add_argument("--src", default="runtime_data/sim/erp.db")
    ap.add_argument("--out", default="runtime_data/legacy/erp.db")
    a = ap.parse_args()
    counts = build(Path(a.src), Path(a.out))
    w = max(len(k) for k in counts)
    for k, v in counts.items():
        print(f"  {k:<{w}}  {v:>7,}")
    print(f"\n  {'total':<{w}}  {sum(counts.values()):>7,}  ->  {a.out}")


if __name__ == "__main__":
    main()
