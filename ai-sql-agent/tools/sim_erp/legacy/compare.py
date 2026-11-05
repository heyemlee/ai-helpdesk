"""
同一批业务事实，两种命名，同一组问题：量化「接真 ERP 到底难多少」。

sim_erp 的字段名是业务白话（job_no、contract_amount、job_status='in_production'），
legacy 是遗留抽取的样子（FBILLNO、FAMOUNT、FDOCSTATUS='C'）。数据一行不差，
语义层用同一套写法（描述 + 别名 + 代码表 + 口径消歧）都写到 100% 覆盖、
validate 零错零警。

两边分数的差距，就是**光靠语义层抹不平的那部分命名鸿沟**——也就是接真 ERP 时
除了写元数据之外还要另想办法的部分。

跑法：起两个服务（或同一个服务换 AI_SQL_SEMANTIC_DIR 重启两次），分别指向
  tools/sim_erp/semantic        → --port 指向它，--mode sim
  tools/sim_erp/legacy/semantic → --port 指向它，--mode legacy
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
import uuid
from pathlib import Path

RUN = uuid.uuid4().hex[:8]
ROOT = Path(__file__).resolve().parents[3]


def _q(db: str, sql: str):
    c = sqlite3.connect(str(ROOT / "runtime_data" / db))
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


# (id, 问题, 考什么, 真值)
def cases() -> list[tuple]:
    sim = "sim/erp.db"
    return [
        ("C1", "J26-00001 这个工程还欠多少钱？", "工程未付总额 = 合同额 − 已收（含未开票的尾款）",
         round(_q(sim, "SELECT contract_amount FROM job WHERE job_no='J26-00001'")
               - _q(sim, """SELECT COALESCE(SUM(p.amount),0) FROM payment_received p
                            JOIN invoice i ON i.invoice_no=p.invoice_no
                            WHERE i.job_no='J26-00001'"""), 2)),
        ("C2", "现在有多少个工程在生产中？", "状态码：sim 是 in_production，legacy 是 FDOCSTATUS='C'",
         _q(sim, "SELECT COUNT(*) FROM job WHERE job_status='in_production'")),
        ("C3", "所有变更单加起来一共增加了多少金额？", "折让是负数，且不能只算已批准",
         round(_q(sim, "SELECT SUM(amount_delta) FROM change_order"), 2)),
        ("C4", "客户的平均信用额度是多少？", "零售客户为 NULL，不是 0",
         round(_q(sim, "SELECT AVG(credit_limit) FROM customer WHERE credit_limit IS NOT NULL"), 2)),
        ("C5", "加州一共有多少个工程？", "州名大小写不统一，要用规范化列",
         _q(sim, "SELECT COUNT(*) FROM job WHERE state_norm='CA'")),
        ("C6", "有多少张发票已经逾期还没收清？", "逾期判据 + 作废不算 + legacy 侧日期是 8 位整数",
         _q(sim, """SELECT COUNT(*) FROM invoice
                    WHERE due_date < DATE('now','localtime')
                      AND invoice_status IN ('open','partial')""")),
        ("C7", "有多少个工程还没下生产工单？", "sim: sold/cancelled；legacy: FDOCSTATUS in (A,Z)",
         _q(sim, "SELECT COUNT(*) FROM job WHERE job_status IN ('sold','cancelled')")),
        ("C8", "商业工程有多少个？", "商业工程的小区字段为 NULL",
         _q(sim, "SELECT COUNT(*) FROM job WHERE subdivision IS NULL")),
    ]


def ask(port: int, question: str, cid: str) -> tuple[object, str]:
    body = json.dumps({"user_id": "cmp", "session_id": f"c-{RUN}-{cid}", "query": question}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/query", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=200) as r:
        d = json.loads(r.read())
    logs = d.get("step_logs") or []
    if not logs or not logs[-1].get("rows"):
        return None, (logs[-1].get("final_sql", "") if logs else "")
    rows = logs[-1]["rows"]
    # 只认单值答案：多行说明它答的不是这个问题
    if len(rows) != 1 or len(rows[0]) != 1:
        return f"<{len(rows)} 行 × {len(rows[0]) if rows else 0} 列>", logs[-1].get("final_sql", "")
    return list(rows[0].values())[0], logs[-1].get("final_sql", "")


def close(a, b) -> bool:
    if a is None or isinstance(a, str):
        return False
    try:
        return abs(float(a) - float(b)) <= max(0.02, abs(float(b)) * 0.001)
    except (TypeError, ValueError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--mode", choices=["sim", "legacy"], required=True)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    results, ok = [], 0
    for cid, question, tests, truth in cases():
        t0 = time.time()
        try:
            got, sql = ask(a.port, question, cid)
            err = None
        except Exception as ex:
            got, sql, err = None, "", f"{type(ex).__name__}: {ex}"
        good = close(got, truth)
        ok += good
        results.append({"id": cid, "问题": question, "考点": tests, "真值": truth,
                        "模型": got, "对": good, "SQL": sql, "错误": err,
                        "耗时s": round(time.time() - t0, 1)})
        print(f"[{cid}] {'✓' if good else '✗'} 真值={truth} 模型={got}", flush=True)

    print(f"\n{a.mode}: {ok}/{len(results)} 正确")
    if a.out:
        Path(a.out).write_text(json.dumps({"mode": a.mode, "score": f"{ok}/{len(results)}",
                                           "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
