"""
边界情况压测：每题一条独立算出的真值，直接比对模型给的数。

**为什么要有真值。** 光看回答"像不像对的"没有意义——这些题的答案都是一个数字，
错的和对的长得一模一样。每题的真值在这里用 SQL 直接从库里算出来，跟模型给的比。

**为什么打 ai-sql-agent 而不是客服。** 这 17 类坑全在 Text2SQL 这一层：NULL、
负数、大小写、状态语义、UTC。绕开客服那一层能少一个变量，也快得多。

用法（需要先起服务，AI_SQL_SEMANTIC_DIR 指向 tools/sim_erp/semantic）：

    python tools/sim_erp/stress.py

**跑之前知道这个：分数会在轮次之间跳。** 同一题两轮可能给出两种都说得通的
解释——这不是模型不稳，是语义层在那个点上留了空当。跳动本身就是信号：哪题跳，
哪题的口径就没定义完。所以这个脚本的用法不是"追求 10/10"，是**看哪题在跳**。

历史上它找到的（都已修）：
  · DATE('now') 是 UTC，加州傍晚之后「今天」偏一天
  · void 作废发票被算进逾期
  · resolved 服务单被算成未结
  · 「所有变更单」被自作主张过滤成只算已批准
  · state 大小写漂移导致按州筛选漏 3%
"""
import json, sqlite3, time, urllib.request, pathlib, uuid

# 每轮用独立 session：复用同一个 session_id 会让 ai-sql-agent 认出「这问题本会话
# 已经答过」，路由到「解释上一条结果」而不是重新查库（那是它的设计，不是故障）。
RUN = uuid.uuid4().hex[:8]

ROOT = pathlib.Path(__file__).resolve().parents[2]
SIM = ROOT / "runtime_data" / "sim"
OUT = ROOT / "runtime_data" / "stress_results.json"

def q(db, sql):
    return sqlite3.connect(SIM / f"{db}.db").execute(sql).fetchone()[0]

# (编号, 问题, 埋的坑, 真值, 算真值的 SQL)
CASES = [
    ("S1", "加州（CA）一共有多少个工程？", "州名大小写不一致：36 行是 Ca",
     q("erp", "SELECT COUNT(*) FROM job WHERE UPPER(state)='CA'"),
     "UPPER(state)='CA'"),
    ("S2", "所有变更单加起来一共增加了多少金额？", "折让是负数，必须 SUM 带符号",
     round(q("erp", "SELECT SUM(amount_delta) FROM change_order"), 2),
     "SUM(amount_delta) 全部"),
    ("S3", "已批准的变更单一共多少钱？", "只能算 approved，pending/rejected 要排除",
     round(q("erp", "SELECT SUM(amount_delta) FROM change_order WHERE co_status='approved'"), 2),
     "co_status='approved'"),
    ("S4", "客户的平均信用额度是多少？", "零售客户 credit_limit 为 NULL，不是 0",
     round(q("erp", "SELECT AVG(credit_limit) FROM customer WHERE credit_limit IS NOT NULL"), 2),
     "AVG 自动跳过 NULL"),
    ("S5", "有多少张发票已经逾期还没收清？", "逾期 = 过期且未 paid，partial 也算",
     q("erp", "SELECT COUNT(*) FROM invoice WHERE due_date < DATE('now','localtime') AND invoice_status IN ('open','partial')"),
     "due_date 过期 且 status in (open,partial)"),
    ("S6", "现在有多少张工单还没完工？", "在制的 completed_date 是 NULL",
     q("prod", "SELECT COUNT(*) FROM work_order WHERE completed_date IS NULL"),
     "completed_date IS NULL"),
    ("S7", "质检里有多少个批次出现过缺陷？", "全合格时 defect_code 为 NULL",
     q("prod", "SELECT COUNT(*) FROM qc_check WHERE defect_code IS NOT NULL"),
     "defect_code IS NOT NULL"),
    ("S8", "有多少笔到货至今还没收到？", "未到货 received_date 为 NULL，比迟到更严重",
     q("wms", "SELECT COUNT(*) FROM receipt WHERE received_date IS NULL"),
     "received_date IS NULL"),
    ("S9", "还没结案的售后单，平均已经开了多少天？", "未结的 resolved_date 为 NULL，不能算进平均处理时长",
     round(q("svc", "SELECT AVG(JULIANDAY(DATE('now','localtime')) - JULIANDAY(reported_date)) FROM service_ticket WHERE resolved_date IS NULL"), 1),
     "只取 resolved_date IS NULL 的，按至今天数"),
    ("S10", "有多少个 SKU 的可用库存低于安全库存？", "可用 = 在库 − 已分配，不是只看在库",
     q("wms", """SELECT COUNT(*) FROM inventory i JOIN material m ON m.sku=i.sku
                 WHERE (i.on_hand_qty - i.allocated_qty) < m.safety_stock"""),
     "(on_hand - allocated) < safety_stock"),

    # ── 补的 7 类：原来 17 类边界情况里没出过题的那些 ──
    ("S11", "有多少个工程还没下生产工单？", "sold/cancelled 不投产，内连接会静默丢掉它们",
     q("erp", "SELECT COUNT(*) FROM job WHERE job_status IN ('sold','cancelled')"),
     "按状态数，或与 prod 左连接后数 NULL"),
    ("S12", "商业工程有多少个？", "商业工程的 subdivision 是 NULL，不是空串",
     q("erp", "SELECT COUNT(*) FROM job WHERE subdivision IS NULL"),
     "subdivision IS NULL（等价于 job_type='commercial'）"),
    ("S13", "有多少个物料从来没有盘点过？", "没盘过的 last_counted 是 NULL",
     q("wms", "SELECT COUNT(*) FROM inventory WHERE last_counted IS NULL"),
     "last_counted IS NULL"),
    ("S14", "现在有多少张工单是挂起状态？", "挂起才有 hold_reason，其余为 NULL",
     q("prod", "SELECT COUNT(*) FROM work_order WHERE wo_status='on_hold'"),
     "wo_status='on_hold'"),
    ("S15", "有多少张发票是部分收款的？", "partial 既不是未付也不是已付，最容易被漏掉",
     q("erp", "SELECT COUNT(*) FROM invoice WHERE invoice_status='partial'"),
     "invoice_status='partial'"),
    ("S16", "有多少个不同的小区？", "商业工程 subdivision 为 NULL，COUNT(DISTINCT) 会自动跳过",
     q("erp", "SELECT COUNT(DISTINCT subdivision) FROM job WHERE subdivision IS NOT NULL"),
     "COUNT(DISTINCT subdivision)，NULL 不计"),
    ("S17", "Sterling Ranch 这个小区有多少个工程？", "lot 号跨小区重复，定位工程必须靠小区名",
     q("erp", "SELECT COUNT(*) FROM job WHERE subdivision='Sterling Ranch'"),
     "subdivision='Sterling Ranch'"),
]

results = []
for cid, question, trap, truth, truth_sql in CASES:
    body = json.dumps({"user_id": "stress", "session_id": f"s-{RUN}-{cid}", "query": question}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/query", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            d = json.loads(r.read())
        err = None
    except Exception as ex:
        d, err = {}, f"{type(ex).__name__}: {ex}"
    dt = time.time() - t0
    steps = d.get("step_logs", [])
    got = None
    if steps and steps[-1].get("rows"):
        vals = list(steps[-1]["rows"][0].values())
        got = vals[0] if len(vals) == 1 else steps[-1]["rows"][0]
    row = {
        "id": cid, "问题": question, "埋的坑": trap,
        "真值": truth, "真值算法": truth_sql, "模型给的": got,
        "对": (isinstance(got, (int, float)) and abs(float(got) - float(truth)) < 0.05),
        "SQL": (steps[-1].get("final_sql") if steps else "").replace("\n", " ")[:260],
        "耗时s": round(dt, 1), "错误": err,
    }
    results.append(row)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    mark = "✓" if row["对"] else "✗"
    print(f"[{cid}] {mark} {dt:5.1f}s  真值={truth}  模型={got}", flush=True)

ok = sum(1 for r in results if r["对"])
print(f"\n{ok}/{len(results)} 正确")
