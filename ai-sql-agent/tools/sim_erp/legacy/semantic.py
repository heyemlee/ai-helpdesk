"""
遗留 ERP 的语义层。

**这个文件就是「接真 ERP」的工作量本身。** 数据和 sim_erp 完全一样，schema 只是
换了命名；两边的问答质量差多少，差的就是这一份翻译。真接一套金蝶/用友时，
产出的东西和这里长得一模一样——只是表更多、字段更多、且没有人记得
FDOCSTATUS='C' 到底是「已审核」还是「已完工」。

三类翻译，缺一不可：
  1. **名字** —— FBILLNO 是工程号、FCUSTID 是客户号。没有它，向量检索对
     这类非自然语言 token 完全无能为力，口语问法一条都召不回
  2. **代码表** —— FDOCSTATUS 的 A/B/C/D/E/F/Z 各是什么。这是最容易漏的一类：
     字段名翻译对了，模型仍然写不出 WHERE FDOCSTATUS='C'，因为它不知道 C 是什么
  3. **口径** —— 三张表都有 FAMOUNT，含义完全不同。光看字段名分不出来
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

Col = tuple[str, str, list[str], str, str]

# 日期是 8 位整数，任何与「今天」的比较都要先转格式。所有 time 字段统一带这句。
_DATE_NOTE = (
    "【日期格式】本列是 8 位整数 20260903，不是 '2026-09-03'。与今天比较要写 "
    "`CAST(STRFTIME('%Y%m%d','now','localtime') AS INTEGER)`，"
    "两个日期列相减不等于天数，要先 DATE(SUBSTR(...)) 转回来。"
)

TABLES: dict[str, tuple[str, list[str], list[Col]]] = {
    "T_SAL_ORDER": (
        "销售订单主表（工程）。一个 FBILLNO 对应一套橱柜的完整交付。",
        ["销售订单", "订单表", "工程表", "job", "订单"],
        [
            ("FBILLNO", "工程号 / 订单号，形如 J25-00123。全公司唯一。",
             ["工程号", "订单号", "单号", "job号", "job number"], "join_key",
             "主键，也是明细、变更单、应收三张表回指本表的键。"),
            ("FCUSTID", "客户编号。", ["客户号", "客户编号"], "join_key", "关联 T_BD_CUSTOMER.FCUSTID。"),
            ("FSUBDIV", "小区名（楼盘）。商业工程为 NULL。", ["小区", "楼盘", "subdivision"], "filter",
             "**商业工程为 NULL**，按小区筛会把整个商业业务筛没。"),
            ("FLOTNO", "地块号。商业工程为 NULL。", ["地块号", "lot", "门牌"], "filter",
             "**lot 号跨小区重复**，单独一个 lot 42 不能唯一定位工程，要配合 FSUBDIV。"),
            ("FPLANNM", "户型名。商业工程为 NULL。", ["户型", "图号", "plan"], "filter", "量产房的标准户型。"),
            ("FADDR", "施工地址。", ["地址", "工地地址"], "text", "送货与安装地址。"),
            ("FCITY", "城市。", ["城市"], "filter", "按区域统计。"),
            ("FPROV", "州的原始录入值。**大小写不统一，不要用它筛选**。",
             ["州原始值"], "dimension", "只用于展示，约 3% 是手工录入的 Ca/Nv。按州筛选用 FPROVSTD。"),
            ("FPROVSTD", "规范化后的州（大写），按州筛选、分组一律用这一列。",
             ["州", "加州", "内华达", "state"], "filter", "等于 UPPER(TRIM(FPROV))。"),
            ("FBIZTYPE", "业务类型：production 量产房 / semi_custom 半定制 / commercial 商业 / service 售后。",
             ["业务类型", "订单类型", "工程类型"], "filter", "四类的金额量级与流程都不同。"),
            ("FAMOUNT", "**合同金额（不含税）**，不含变更单增减。",
             ["合同额", "合同金额", "订单金额", "金额"], "metric",
             "**口径警告**：本库三张表都有 FAMOUNT，含义完全不同——"
             "本列是订单头的合同额；T_SAL_ORDERENTRY.FAMOUNT 是明细行金额；"
             "T_AR_RECEIVABLE.FAMOUNT 是单张应收的开票额。问「合同额」用本列。"),
            ("FDATE", f"签单日期。{_DATE_NOTE}", ["签单日", "下单日期", "签单时间"], "time", "按月/季统计签单量。"),
            ("FDELIVDATE", f"计划交付日期。未下达或已取消时为 NULL。{_DATE_NOTE}",
             ["计划交付日", "交期", "计划安装日"], "time", "**未下达和已取消的为 NULL**。"),
            ("FDOCSTATUS", "单据状态**代码**。", ["订单状态", "单据状态", "状态"], "filter",
             "**代码表（这是接遗留 ERP 最容易漏的一类翻译）**：\n"
             "  A=已签未下达 / B=已下达 / C=生产中 / D=已发货 / E=已安装 / F=已结案 / Z=已取消。\n"
             "「在产的」是 C；「已交付的」是 D、E、F；「还没投产的」是 A 和 Z。\n"
             "**A 和 Z 没有生产工单**，跟生产库做内连接会静默丢掉约 14% 的订单。"),
        ],
    ),
    "T_SAL_ORDERENTRY": (
        "销售订单明细行。一个工程下的每一项柜体、台面、五金、线条。",
        ["订单明细", "明细行", "行项目", "配置单"],
        [
            ("FENTRYID", "明细行号。", ["明细号"], "join_key", "主键。"),
            ("FBILLNO", "所属工程号。", ["工程号", "订单号"], "join_key", "关联 T_SAL_ORDER.FBILLNO。"),
            ("FSEQ", "行序号。", ["行号"], "dimension", "工程内排序。"),
            ("FMATCAT", "物料品类：base_cabinet 地柜 / wall_cabinet 吊柜 / tall_cabinet 高柜 / "
                        "vanity 浴室柜 / countertop 台面 / hardware 五金 / molding 线条 / accessory 配件。",
             ["品类", "类别", "柜型"], "filter", "按品类统计用量或金额。"),
            ("FMATNO", "物料编码。", ["物料号", "编码", "sku"], "join_key", "物料主数据的键。"),
            ("FMATNAME", "规格描述。", ["规格", "物料名", "描述"], "text", "给人看的规格文字。"),
            ("FQTY", "数量。", ["数量", "件数"], "metric", "行数量。"),
            ("FPRICE", "单价（不含税）。", ["单价"], "metric", "单件价格。"),
            ("FAMOUNT", "**行金额** = FPRICE × FQTY（不含税）。",
             ["行金额", "小计"], "metric",
             "**口径警告**：与 T_SAL_ORDER.FAMOUNT（整单合同额）不是一回事。"
             "行金额合计也不等于合同额——合同额是谈下来的总价，明细是配置清单，中间有折扣。"),
        ],
    ),
    "T_SAL_CHGORDER": (
        "变更单。签约后对配置、尺寸、饰面的改动，或退让给建筑商的折让。",
        ["变更单", "改单", "change order", "加减项"],
        [
            ("FCHGNO", "变更单号。", ["变更单号", "CO号"], "join_key", "主键。"),
            ("FBILLNO", "所属工程号。", ["工程号", "订单号"], "join_key", "关联 T_SAL_ORDER.FBILLNO。"),
            ("FCHGRSN", "变更原因：door_style_change 换门型 / added_cabinets 加柜 / size_change 改尺寸 / "
                        "finish_change 改饰面 / builder_credit 折让 / field_conflict 现场冲突。",
             ["变更原因", "原因"], "filter", "按原因归类。"),
            ("FCHGAMT", "金额增减（不含税）。**折让为负数**。",
             ["变更金额", "增减", "加价"], "metric",
             "**有正有负**，求变更总额必须 SUM(FCHGAMT)，取绝对值或只取正数都算错。"),
            ("FAPPLYDATE", f"提出日期。{_DATE_NOTE}", ["提出日", "申请日期"], "time", "变更提出时间。"),
            ("FAUDITDATE", f"审批日期。待审时为 NULL。{_DATE_NOTE}", ["批准日", "审批日期"], "time",
             "**待审为 NULL**，统计审批时长必须排除。"),
            ("FDOCSTATUS", "变更单状态**代码**。", ["变更状态", "审批状态"], "filter",
             "**代码表**：A=待审 / C=已批准 / R=已驳回。\n"
             "**算工程实际金额时只计 C**；但问「所有变更单」「一共提过多少变更」时指的是全部，"
             "不要自作主张加状态过滤。"),
        ],
    ),
    "T_AR_RECEIVABLE": (
        "应收单（发票）。按定金 / 进度款 / 尾款三段开票。",
        ["应收", "发票", "账单", "开票"],
        [
            ("FARNO", "应收单号（发票号）。", ["发票号", "应收单号"], "join_key", "主键。"),
            ("FBILLNO", "所属工程号。", ["工程号", "订单号"], "join_key", "关联 T_SAL_ORDER.FBILLNO。"),
            ("FARTYPE", "款项类型：deposit 定金 / progress 进度款 / final 尾款。",
             ["款项类型", "发票类型", "定金", "尾款"], "filter", "问「尾款」指 FARTYPE='final'。"),
            ("FBILLDATE", f"开票日期。{_DATE_NOTE}", ["开票日"], "time", "开票时间。"),
            ("FAMOUNT", "**本张应收的开票金额**（不含税）。",
             ["开票金额", "应收金额", "发票金额"], "metric",
             "**口径警告**：与 T_SAL_ORDER.FAMOUNT（整单合同额）不同。"
             "已开票合计通常小于合同额——尾款常常还没开。"),
            ("FDUEDATE", f"到期日。{_DATE_NOTE}", ["到期日", "账期到期"], "time",
             "**逾期判据**：FDUEDATE < 今天 且 FSETTLESTS 不是 S。"),
            ("FSETTLESTS", "结算状态**代码**。", ["结算状态", "收款状态", "发票状态"], "filter",
             "**代码表**：O=未收 / P=部分收款 / S=已收清 / V=作废。\n"
             "「未收清」= O 或 P，**不含 V**（作废不构成应收，算进去会虚高）。"
             "P 最容易漏：它既不是未付也不是已付，未付余额要用开票额减实收合计。"),
        ],
    ),
    "T_AR_RECEIVEBILL": (
        "收款单。客户实际到账的每一笔，一张应收可能分多笔收。",
        ["收款", "回款", "到账", "客户付款"],
        [
            ("FRECNO", "收款单号。", ["收款单号", "流水号"], "join_key", "主键。"),
            ("FARNO", "对应应收单号。", ["发票号", "应收单号"], "join_key",
             "关联 T_AR_RECEIVABLE.FARNO。**未付余额 = 应收的 FAMOUNT − 该应收下本表 FAMOUNT 的合计**，"
             "只看应收单一行读不出余额。"),
            ("FAMOUNT", "**本笔到账金额**。", ["收款金额", "到账金额"], "metric", "单笔实收。"),
            ("FRECDATE", f"到账日期。{_DATE_NOTE}", ["到账日", "收款日期"], "time",
             "**逾期回款判据**：FRECDATE > 对应应收的 FDUEDATE。"),
            ("FSETTLETYPE", "结算方式：check 支票 / ach / credit_card 信用卡 / wire 电汇。",
             ["收款方式", "付款方式"], "dimension", "到账渠道。"),
        ],
    ),
    "T_BD_CUSTOMER": (
        "客户主数据。建筑商、总包、经销商、零售业主四类。",
        ["客户", "客户主数据", "builder"],
        [
            ("FCUSTID", "客户编号。", ["客户号", "客户编号"], "join_key", "主键。"),
            ("FNAME", "客户名称。", ["客户名", "公司名", "客户"], "filter",
             "**存在近似重名**（Marlowe Homes / Marlowe Homes LLC），精确匹配会漏、模糊匹配会多。"),
            ("FCUSTTYPE", "客户类型**代码**。", ["客户类型", "客户性质"], "filter",
             "**代码表**：01=量产建筑商 / 02=总包 / 03=经销商 / 04=零售业主。"),
            ("FPAYCOND", "付款条件**代码**。", ["付款条件", "账期"], "dimension",
             "**代码表**：M30/M45/M60=月结 30/45/60 天，D50=五成定金。决定应收的到期日。"),
            ("FCREDITLMT", "信用额度。零售客户没有授信，本列为 NULL。",
             ["信用额度", "授信"], "metric", "**零售为 NULL 不是 0**：AVG/SUM 会跳过，`> x` 也筛不到。"),
            ("FFORBIDSTS", "禁用状态**代码**。", ["是否禁用", "是否活跃", "状态"], "filter",
             "**代码表**：A=正常 / B=已禁用。问「我们有多少客户」通常要 FFORBIDSTS='A'。"),
        ],
    ),
}


def _entry(table: str, col: Col, samples: list[str]) -> dict:
    name, desc, aliases, role, usage = col
    if role == "time":
        pass  # 日期说明已经写进 desc，避免和 _DATE_NOTE 重复
    alias_line = " ".join(aliases)
    e = {
        "description": desc,
        "aliases": aliases,
        "semantic_role": role,
        "business_usage": usage,
        # 遗留字段名是非自然语言 token，向量检索基本无能为力，全靠 keyword_text
        # 里的业务口语别名让 BM25 那一路命中。这一条是接遗留 ERP 的关键。
        "keyword_text": f"{name} {alias_line} {table}",
        "vector_text": f"字段名：{name}。所属表：{table}。{desc}",
        "rerank_text": f"字段：{table}.{name}。含义：{desc} 角色：{role}。",
    }
    if samples:
        e["samples"] = samples
    return e


def build_semantic(db: Path, out_dir: Path) -> list[str]:
    import sqlite3
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))

    def samples(table: str, col: str) -> list[str]:
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL "
                f"ORDER BY {col} LIMIT 4").fetchall()
        except Exception:
            return []
        return [str(v) for (v,) in rows if len(str(v)) <= 40]

    doc = {
        "name": "erp_db",
        "description": "遗留 ERP 抽取库（金蝶/用友式命名）：销售订单、订单明细、变更单、"
                       "应收与收款、客户主数据。**字段名是 F 前缀缩写，状态是单字母代码，"
                       "日期是 8 位整数**——业务含义全在本元数据里，库本身没有注释。",
        "aliases": ["ERP", "ERP库", "订单库", "应收库", "遗留ERP"],
        "connection": {"dialect": "sqlite", "db_path": str(db)},
        "tables": {
            t: {"description": d, "aliases": a,
                "columns": {c[0]: _entry(t, c, samples(t, c[0])) for c in cols}}
            for t, (d, a, cols) in TABLES.items()
        },
    }
    conn.close()
    p = out_dir / "erp_db.yaml"
    p.write_text("# 遗留 ERP 语义元数据 —— 由 tools/sim_erp/legacy/semantic.py 生成，不要手改。\n"
                 + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
                 encoding="utf-8")

    shared = {
        "metric_disambiguation": [
            {"term": "合同额 / 合同金额 / 订单金额",
             "correct": "erp_db.T_SAL_ORDER.FAMOUNT",
             "wrong": "erp_db.T_SAL_ORDERENTRY.FAMOUNT（明细行金额，三张表都叫 FAMOUNT，口径完全不同）"},
            {"term": "开票金额 / 应收金额",
             "correct": "erp_db.T_AR_RECEIVABLE.FAMOUNT",
             "wrong": "erp_db.T_SAL_ORDER.FAMOUNT（整单合同额；已开票合计通常小于它，尾款常常没开）"},
            {"term": "还欠多少 / 未付总额",
             "correct": "erp_db.T_SAL_ORDER.FAMOUNT",
             "wrong": "erp_db.T_AR_RECEIVABLE.FAMOUNT（只汇总已开票的会漏掉尚未开票的尾款）"},
            {"term": "实收 / 到账",
             "correct": "erp_db.T_AR_RECEIVEBILL.FAMOUNT",
             "wrong": "erp_db.T_AR_RECEIVABLE.FSETTLESTS（状态只有 O/P/S/V，读不出收了多少钱）"},
            {"term": "按州筛选",
             "correct": "erp_db.T_SAL_ORDER.FPROVSTD",
             "wrong": "erp_db.T_SAL_ORDER.FPROV（原始录入值，大小写不统一）"},
        ],
        "metric_definitions": {
            "在产": "FDOCSTATUS='C'（生产中）。A/B 还没投产，D/E/F 已交付，Z 已取消。",
            "未收清应收": "FSETTLESTS 为 O 或 P 的应收单。**不含 V**——作废不构成应收。",
            "逾期应收": "FDUEDATE < 今天（8 位整数比较）且 FSETTLESTS 不是 S 也不是 V。",
            "单张应收未付余额": "该应收的 FAMOUNT 减去 T_AR_RECEIVEBILL 里对应 FARNO 的 FAMOUNT 合计。",
            "工程未付总额": "T_SAL_ORDER.FAMOUNT + 该工程 FDOCSTATUS='C' 的变更单 SUM(FCHGAMT) "
                       "− 该工程所有应收下的收款合计。**客户问「还欠多少」指的是这个**，"
                       "因为它包含了尚未开票的尾款。",
            "今天": "本库日期是 8 位整数，今天要写 "
                  "CAST(STRFTIME('%Y%m%d','now','localtime') AS INTEGER)。",
        },
    }
    sp = out_dir / "_shared.yaml"
    sp.write_text("# 跨表共享语义 —— 由 tools/sim_erp/legacy/semantic.py 生成，不要手改。\n"
                  + yaml.safe_dump(shared, allow_unicode=True, sort_keys=False, width=100),
                  encoding="utf-8")
    return [p.name, sp.name]


def main() -> None:
    ap = argparse.ArgumentParser(description="生成遗留 ERP 的语义层。")
    ap.add_argument("--db", default="runtime_data/legacy/erp.db")
    ap.add_argument("--out", default="tools/sim_erp/legacy/semantic")
    a = ap.parse_args()
    for n in build_semantic(Path(a.db), Path(a.out)):
        print(f"  {n}")
    print(f"\n  -> {a.out}")


if __name__ == "__main__":
    main()
