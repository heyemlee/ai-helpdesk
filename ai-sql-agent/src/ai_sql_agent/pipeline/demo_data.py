from __future__ import annotations

import functools
import sqlite3
from pathlib import Path


def _create_database_from_sql(db_path: str | Path, sql_file: str) -> Path:
    """从包内 resources/demo_sql/ 下的建库脚本创建测试数据库。"""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    sql_dir = Path(__file__).resolve().parents[1] / "resources" / "demo_sql"
    sql_text = (sql_dir / sql_file).read_text(encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql_text)
    conn.commit()
    conn.close()
    return db_path


def create_erp_demo_database(db_path: str | Path) -> Path:
    """ERP：订单与采购域（sales_order / supplier / purchase_order / payment）。"""
    return _create_database_from_sql(db_path, "create_erp_demo.sql")


def create_pm_demo_database(db_path: str | Path) -> Path:
    """项目管理：项目与工期域（project / milestone / change_order / acceptance）。"""
    return _create_database_from_sql(db_path, "create_pm_demo.sql")


def create_mes_demo_database(db_path: str | Path) -> Path:
    """生产系统：生产与工时域（work_order / labor_hour / capacity_stat / quality_check）。"""
    return _create_database_from_sql(db_path, "create_mes_demo.sql")


def create_wms_demo_database(db_path: str | Path) -> Path:
    """仓储：库存与物料域（material / batch / stock_snapshot）。"""
    return _create_database_from_sql(db_path, "create_wms_demo.sql")


# 契约 §4.6.5：演示元数据的字段单位。
#
# 为什么要给**演示**元数据配单位：默认部署（没配 AI_SQL_SEMANTIC_DIR）用的就是它，
# 而它没有 unit 就意味着 `currency` 恒为 null，于是调用方永远退到自己的
# `SQL_AGENT_CURRENCY` 全局常量——那条路径已经出过一次业务事故
# （中文回答默认写「元」，接美国厂的库时金额带错币种）。
# 「契约上关掉了、默认部署下还开着」不算关掉。
#
# 币种取 CNY：这套演示数据是中文的橱柜厂场景（客户/订单/工单/车间），
# 描述里没写币种，所以这是**为虚构数据显式声明**，不是从哪里推出来的。
# 接真实业务库必须在自己的语义层 YAML 里声明自己的币种，别沿用这里。
#
# 只写有把握的：金额 → CNY，件数 → EA，描述里明写 (0-1) 的比率 → ratio。
# 拿不准的一律不写——契约 §4.6.5 说得很清楚，缺失是「单位未知」，
# 调用方不得擅自补，猜一个填进去比留空危险得多。
COLUMN_UNITS: dict = {
    "erp_db.sales_order.order_amt": "CNY",
    "erp_db.purchase_order.po_amt": "CNY",
    "erp_db.payment.paid_amt": "CNY",
    "pm_db.project.contract_amt": "CNY",
    "pm_db.project.budget_amt": "CNY",
    "pm_db.project.actual_cost": "CNY",
    "pm_db.change_order.change_amt": "CNY",
    "wms_db.batch.unit_cost": "CNY",
    "mes_db.work_order.plan_qty": "EA",
    "mes_db.capacity_stat.planned_qty": "EA",
    "mes_db.capacity_stat.actual_qty": "EA",
    "mes_db.quality_check.checked_qty": "EA",
    "mes_db.quality_check.passed_qty": "EA",
    "wms_db.batch.in_qty": "EA",
    "wms_db.stock_snapshot.on_hand_qty": "EA",
    "wms_db.stock_snapshot.locked_qty": "EA",
    "erp_db.supplier.on_time_rate": "ratio",
    "mes_db.quality_check.yield_rate": "ratio",
}


def _apply_units(database: str, tables: dict) -> dict:
    """
    把 COLUMN_UNITS 里属于这个库的单位写进表元数据。

    键写错会**静默不生效**——单位没下发，调用方回退到全局币种常量，
    金额照样报出去、看起来完全正常。所以这里对不上就直接抛，
    与 tools/sim_erp/semantic.py 的 _assert_units_apply 是同一条理由。
    """
    known = {
        f"{database}.{table}.{column}"
        for table, meta in tables.items()
        for column in (meta.get("columns") or {})
    }
    stale = sorted(
        key for key in COLUMN_UNITS
        if key.startswith(f"{database}.") and key not in known
    )
    if stale:
        raise ValueError(
            f"COLUMN_UNITS 里这些键在 {database} 里找不到对应字段"
            f"（写错会静默不生效）：{'、'.join(stale)}"
        )

    for table, meta in tables.items():
        for column, column_meta in (meta.get("columns") or {}).items():
            unit = COLUMN_UNITS.get(f"{database}.{table}.{column}")
            if unit and isinstance(column_meta, dict):
                column_meta["unit"] = unit
    return tables



def _with_units(database: str):
    """
    给 get_*_business_meta 挂单位。

    单位写在这里而不是语义层加载器里：`demo_data` 是演示元数据的**单一事实来源**，
    加载器只是换一个入口。在加载器里加工会让
    `layer.business_metas()["erp_db"] == get_erp_business_meta()` 这条不变量失效
    ——那条不变量有专门的用例守着（tests/test_semantic_layer.py），它是对的。
    """

    def decorate(builder):
        @functools.wraps(builder)
        def wrapped() -> dict:
            return _apply_units(database, builder())

        return wrapped

    return decorate


@_with_units("erp_db")
def get_erp_business_meta() -> dict:
    """
    ERP 域业务元数据。

    注意 job_no、po_amt 这类 ERP 遗留缩写命名：字面无业务语义，
    向量检索对这类非自然语言 token 表现差，必须靠 keyword_text 里
    补齐业务口语别名，让 BM25 那一路能命中。
    """
    return {
        "sales_order": {
            "description": "销售订单表，记录客户签订的订单，含工程号、客户名称、含税订单金额、签单日期与订单状态。",
            "aliases": ["销售订单表", "订单表", "签单表"],
            "columns": {
                "order_no": {
                    "description": "销售订单号。",
                    "aliases": ["订单号", "订单编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识销售订单。",
                },
                "job_no": {
                    "description": "工程号，ERP 内部编码。与项目管理域的 project_code 指同一工程，但两库无外键约束。",
                    "aliases": ["工程号", "工程编号", "项目号", "job号"],
                    "semantic_role": "join_key",
                    "business_usage": "跨库关联键：按命名约定对应 pm_db.project.project_code。",
                    "samples": ["J24001", "J24003", "J24007"],
                    "keyword_text": "job_no 工程号 工程编号 项目号 job号 跨库关联 ERP编码",
                    "vector_text": "字段名：job_no。所属表：sales_order。该字段是 ERP 的工程号编码，对应项目管理系统中的项目编号。",
                    "rerank_text": "字段：sales_order.job_no。含义：工程号（ERP 缩写码）。角色：join_key，跨库关联到 project.project_code。",
                },
                "customer_name": {
                    "description": "客户名称，零售客户或工程项目名。",
                    "aliases": ["客户", "客户名称", "甲方"],
                    "semantic_role": "dimension",
                    "business_usage": "按客户筛选或展示订单。",
                },
                "order_amt": {
                    "description": "订单金额，**含税**。与项目管理域的 contract_amt（不含税合同额）口径不同，不可直接比较。",
                    "aliases": ["订单金额", "签单金额", "成交金额", "销售额"],
                    "semantic_role": "metric_filter",
                    "value_range": "> 0",
                    "business_usage": "衡量订单规模；跨库与合同额比较时需注意含税/不含税差异。",
                    "samples": ["97180.00", "429400.00", "2373000.00"],
                    "keyword_text": "order_amt 订单金额 签单金额 成交金额 销售额 含税 大于",
                    "vector_text": "字段名：order_amt。所属表：sales_order。该字段表示含税的订单成交金额，用于衡量订单规模。",
                    "rerank_text": "字段：sales_order.order_amt。含义：含税订单金额。别名：订单金额、签单金额、成交金额。注意：含税口径，与 project.contract_amt（不含税）不同。",
                },
                "sign_date": {
                    "description": "签单日期。",
                    "aliases": ["签单日期", "签约日期", "下单日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间区间筛选订单。",
                },
                "order_status": {
                    "description": "订单状态：signed 已签单 / in_production 生产中 / delivered 已交付。",
                    "aliases": ["订单状态", "状态"],
                    "semantic_role": "filter",
                    "value_range": "signed / in_production / delivered",
                    "business_usage": "筛选各状态订单。",
                },
            },
        },
        "purchase_order": {
            "description": "采购单表，记录向供应商下达的采购单，含工程号、供应商、含税采购金额、采购日期与到货状态。",
            "aliases": ["采购单表", "采购订单表", "PO表"],
            "columns": {
                "po_no": {
                    "description": "采购单号。",
                    "aliases": ["采购单号", "PO号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识采购单，关联付款表。",
                },
                "job_no": {
                    "description": "工程号，标明这笔采购归属哪个工程。",
                    "aliases": ["工程号", "工程编号", "项目号"],
                    "semantic_role": "join_key",
                    "business_usage": "按工程归集采购成本；跨库对应 project.project_code。",
                    "keyword_text": "job_no 工程号 工程编号 项目号 采购归属",
                    "vector_text": "字段名：job_no。所属表：purchase_order。该字段表示采购单归属的工程编号。",
                },
                "supplier_id": {
                    "description": "供应商编号。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联供应商表。",
                },
                "po_amt": {
                    "description": "采购金额，**含税**。ERP 缩写命名，字面无业务语义。",
                    "aliases": ["采购金额", "采购额", "进货金额", "采购成本"],
                    "semantic_role": "metric_filter",
                    "value_range": "> 0",
                    "data_distribution": "零售工程数万元，批量工程可达数十万。",
                    "business_usage": "统计采购支出、按工程归集材料成本。",
                    "samples": ["26000.00", "186000.00", "742000.00"],
                    "keyword_text": "po_amt 采购金额 采购额 进货金额 采购成本 含税 采购单表 PO表",
                    "vector_text": "字段名：po_amt。所属表：purchase_order。该字段表示含税的采购金额，用于统计采购支出与工程材料成本。",
                    "rerank_text": "字段：purchase_order.po_amt。含义：含税采购金额（ERP 缩写码）。别名：采购金额、采购额、采购成本。用途：统计采购支出或按工程归集成本。",
                },
                "po_date": {
                    "description": "采购下单日期。",
                    "aliases": ["采购日期", "下单日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间区间统计采购。",
                },
                "po_status": {
                    "description": "采购单状态：ordered 已下单 / partial 部分到货 / received 已到货。",
                    "aliases": ["采购状态", "到货状态"],
                    "semantic_role": "filter",
                    "value_range": "ordered / partial / received",
                    "business_usage": "筛选未到货或已到货的采购单。",
                },
            },
        },
        "supplier": {
            "description": "供应商表，记录供应商名称、供货品类与准时交付率。",
            "aliases": ["供应商表", "供方表"],
            "columns": {
                "supplier_id": {
                    "description": "供应商编号。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识供应商。",
                },
                "supplier_name": {
                    "description": "供应商名称。",
                    "aliases": ["供应商", "供应商名称", "供方名称"],
                    "semantic_role": "output_dimension",
                    "samples": ["华鹏板材", "东顺五金", "杰森石英石"],
                    "keyword_text": "supplier_name 供应商 供应商名称 供方 厂家",
                    "vector_text": "字段名：supplier_name。所属表：supplier。该字段表示供应商名称，常作为查询结果输出。",
                    "rerank_text": "字段：supplier.supplier_name。含义：供应商名称。用途：询问是哪家供应商时作为输出字段。",
                },
                "category": {
                    "description": "供货品类：板材 / 五金 / 台面 / 涂料 / 玻璃。",
                    "aliases": ["品类", "供货品类", "材料类别", "供货类别"],
                    "semantic_role": "dimension",
                    "value_range": "板材 / 五金 / 台面 / 涂料 / 玻璃",
                    "samples": ["板材", "五金", "台面", "涂料", "玻璃"],
                    "business_usage": "按品类筛选供应商。",
                    "keyword_text": "category 品类 供货品类 材料类别 板材 五金 台面 涂料 玻璃 板材类 五金类 台面类",
                    "vector_text": "字段名：category。所属表：supplier。该字段表示供应商的供货品类，取值为板材、五金、台面、涂料、玻璃。",
                    "rerank_text": "字段：supplier.category。含义：供货品类。取值：板材、五金、台面、涂料、玻璃。用途：按品类筛选供应商，用户常以取值提问如「板材类供应商」。",
                },
                "on_time_rate": {
                    "description": "准时交付率，0-1 之间的小数。",
                    "aliases": ["准时率", "准时交付率", "及时率", "交付准时率"],
                    "semantic_role": "metric_filter",
                    "value_range": "0-1",
                    "business_usage": "评估供应商交付表现，筛选准时率低的供应商。",
                    "samples": ["0.65", "0.88", "0.95"],
                    "keyword_text": "on_time_rate 准时率 准时交付率 及时率 交付准时率 供应商表现",
                    "vector_text": "字段名：on_time_rate。所属表：supplier。该字段表示供应商的准时交付率，用于评估供应商表现。",
                    "rerank_text": "字段：supplier.on_time_rate。含义：准时交付率（0-1）。别名：准时率、及时率。用途：筛选准时率低于阈值的供应商。",
                },
            },
        },
        "payment": {
            "description": "付款表，记录针对采购单的付款金额、付款日期与付款状态。",
            "aliases": ["付款表", "付款记录表"],
            "columns": {
                "payment_id": {
                    "description": "付款记录编号。",
                    "aliases": ["付款编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识付款记录。",
                },
                "po_no": {
                    "description": "关联的采购单号。",
                    "aliases": ["采购单号", "PO号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联采购单表。",
                },
                "paid_amt": {
                    "description": "已付金额。",
                    "aliases": ["已付金额", "付款金额", "实付金额"],
                    "semantic_role": "metric",
                    "business_usage": "统计已付款金额、核对应付未付。",
                    "keyword_text": "paid_amt 已付金额 付款金额 实付金额 回款",
                    "vector_text": "字段名：paid_amt。所属表：payment。该字段表示针对采购单已支付的金额。",
                },
                "paid_date": {
                    "description": "付款日期。",
                    "aliases": ["付款日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间统计付款。",
                },
                "pay_status": {
                    "description": "付款状态：paid 已付清 / partial 部分支付。",
                    "aliases": ["付款状态"],
                    "semantic_role": "filter",
                    "value_range": "paid / partial",
                    "business_usage": "筛选未付清的采购单。",
                },
            },
        },
    }


@_with_units("pm_db")
def get_pm_business_meta() -> dict:
    """项目管理域业务元数据。"""
    return {
        "project": {
            "description": "项目表，记录工程项目的名称、所属板块、不含税合同额、预算、实际成本、工期与状态。",
            "aliases": ["项目表", "工程表", "项目信息表"],
            "columns": {
                "project_code": {
                    "description": "项目编号。与 ERP 域的 job_no 指同一工程，两库间无外键，靠命名约定关联。",
                    "aliases": ["项目编号", "项目号", "工程号"],
                    "semantic_role": "join_key",
                    "business_usage": "跨库关联键：按命名约定对应 erp_db 的 job_no。",
                    "samples": ["J24001", "J24003", "J24007"],
                    "keyword_text": "project_code 项目编号 项目号 工程号 跨库关联",
                    "vector_text": "字段名：project_code。所属表：project。该字段是项目编号，对应 ERP 系统中的工程号 job_no。",
                    "rerank_text": "字段：project.project_code。含义：项目编号。角色：join_key，跨库对应 ERP 的 job_no。",
                },
                "project_name": {
                    "description": "项目名称。",
                    "aliases": ["项目名称", "项目名", "工程名称"],
                    "semantic_role": "output_dimension",
                    "samples": ["万科精装1号楼橱柜工程", "园区消防管道改造"],
                    "keyword_text": "project_name 项目名称 项目名 工程名称",
                    "vector_text": "字段名：project_name。所属表：project。该字段表示项目名称，常作为查询结果输出。",
                    "rerank_text": "字段：project.project_name。含义：项目名称。用途：询问哪些项目时作为输出字段。",
                },
                "business_unit": {
                    "description": "所属业务板块：建筑装修 / 消防 / 地产 / 教育。",
                    "aliases": ["业务板块", "板块", "子公司", "事业部"],
                    "semantic_role": "dimension",
                    "value_range": "建筑装修 / 消防 / 地产 / 教育",
                    "business_usage": "按板块分组统计，集团多子公司口径。",
                    "keyword_text": "business_unit 业务板块 板块 子公司 事业部 建筑装修 消防",
                    "vector_text": "字段名：business_unit。所属表：project。该字段表示项目所属的业务板块或子公司。",
                },
                "contract_amt": {
                    "description": "合同额，**不含税**。与 ERP 的 order_amt/po_amt（含税）口径不同，跨库比较需注意。",
                    "aliases": ["合同额", "合同金额", "项目金额"],
                    "semantic_role": "metric_filter",
                    "value_range": "> 0",
                    "business_usage": "衡量项目规模；与实际成本比较算毛利。",
                    "samples": ["86000.00", "680000.00", "2100000.00"],
                    "keyword_text": "contract_amt 合同额 合同金额 项目金额 不含税",
                    "vector_text": "字段名：contract_amt。所属表：project。该字段表示不含税的项目合同额。",
                    "rerank_text": "字段：project.contract_amt。含义：不含税合同额。注意：不含税口径，与 ERP 的 order_amt（含税）不同，不可直接比较。",
                },
                "budget_amt": {
                    "description": "项目预算金额。",
                    "aliases": ["预算", "预算金额", "项目预算"],
                    "semantic_role": "metric_filter",
                    "business_usage": "与实际成本比较判断是否超预算。",
                    "keyword_text": "budget_amt 预算 预算金额 项目预算 超预算",
                    "vector_text": "字段名：budget_amt。所属表：project。该字段表示项目预算金额，与实际成本比较可判断是否超预算。",
                    "rerank_text": "字段：project.budget_amt。含义：项目预算金额。用途：与 actual_cost 比较判断超预算。",
                },
                "actual_cost": {
                    "description": "项目实际发生成本。",
                    "aliases": ["实际成本", "实际花费", "已发生成本"],
                    "semantic_role": "metric_filter",
                    "business_usage": "与预算比较判断超支；与合同额比较算毛利。",
                    "keyword_text": "actual_cost 实际成本 实际花费 已发生成本 超预算 超支",
                    "vector_text": "字段名：actual_cost。所属表：project。该字段表示项目实际发生的成本，超过预算即为超预算项目。",
                    "rerank_text": "字段：project.actual_cost。含义：实际成本。用途：actual_cost > budget_amt 即超预算。",
                },
                "start_date": {
                    "description": "项目开工日期。",
                    "aliases": ["开工日期", "开始日期", "启动日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间区间筛选项目。",
                },
                "due_date": {
                    "description": "项目计划完工日期。",
                    "aliases": ["计划完工日期", "交付日期", "工期截止"],
                    "semantic_role": "time",
                    "business_usage": "判断是否延期。",
                },
                "project_status": {
                    "description": "项目状态：active 进行中 / completed 已完成。",
                    "aliases": ["项目状态", "状态"],
                    "semantic_role": "filter",
                    "value_range": "active / completed",
                    "business_usage": "筛选进行中或已完成的项目。",
                },
            },
        },
        "milestone": {
            "description": "里程碑表，记录项目关键节点的计划日期、实际日期与达成状态。",
            "aliases": ["里程碑表", "节点表", "工期节点表"],
            "columns": {
                "milestone_id": {
                    "description": "里程碑编号。",
                    "aliases": ["里程碑编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识里程碑。",
                },
                "project_code": {
                    "description": "所属项目编号。",
                    "aliases": ["项目编号", "项目号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联项目表。",
                },
                "milestone_name": {
                    "description": "里程碑名称，如深化图纸确认、首批柜体进场、整体验收。",
                    "aliases": ["里程碑", "节点名称"],
                    "semantic_role": "dimension",
                    "business_usage": "按节点类型筛选。",
                },
                "plan_date": {
                    "description": "计划达成日期。",
                    "aliases": ["计划日期", "计划完成日期"],
                    "semantic_role": "time",
                    "business_usage": "与实际日期比较判断延期。",
                },
                "actual_date": {
                    "description": "实际达成日期，未达成为空。",
                    "aliases": ["实际日期", "实际完成日期"],
                    "semantic_role": "time",
                    "business_usage": "判断节点是否已达成。",
                },
                "ms_status": {
                    "description": "里程碑状态：on_time 准时 / delayed 延期 / pending 未开始。",
                    "aliases": ["节点状态", "里程碑状态", "延期状态"],
                    "semantic_role": "filter",
                    "value_range": "on_time / delayed / pending",
                    "business_usage": "筛选延期节点。",
                    "keyword_text": "ms_status 节点状态 里程碑状态 延期 delayed 准时 on_time",
                    "vector_text": "字段名：ms_status。所属表：milestone。该字段表示里程碑是准时、延期还是未开始。",
                },
            },
        },
        "change_order": {
            "description": "变更单表，记录项目变更的金额、原因与审批日期。",
            "aliases": ["变更单表", "变更表", "签证表", "加签表", "增项表"],
            "columns": {
                "change_id": {
                    "description": "变更单编号。",
                    "aliases": ["变更单号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识变更单。",
                },
                "project_code": {
                    "description": "所属项目编号。",
                    "aliases": ["项目编号", "项目号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联项目表。",
                },
                "change_amt": {
                    "description": "变更金额。",
                    "aliases": ["变更金额", "签证金额", "增项金额", "增项", "加签"],
                    "semantic_role": "metric",
                    "business_usage": "统计项目变更增减。",
                    "keyword_text": "change_amt 变更金额 签证金额 增项金额 变更单",
                    "vector_text": "字段名：change_amt。所属表：change_order。该字段表示项目变更单的金额。",
                },
                "change_reason": {
                    "description": "变更原因。",
                    "aliases": ["变更原因", "原因"],
                    "semantic_role": "dimension",
                    "business_usage": "分析变更成因。",
                },
                "approved_date": {
                    "description": "审批通过日期，未审批为空。",
                    "aliases": ["审批日期"],
                    "semantic_role": "time",
                    "business_usage": "筛选未审批的变更单。",
                },
            },
        },
        "acceptance": {
            "description": "验收表，记录项目验收日期与验收结果。",
            "aliases": ["验收表", "验收记录表"],
            "columns": {
                "acceptance_id": {
                    "description": "验收记录编号。",
                    "aliases": ["验收编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识验收记录。",
                },
                "project_code": {
                    "description": "所属项目编号。",
                    "aliases": ["项目编号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联项目表。",
                },
                "accept_date": {
                    "description": "验收日期。",
                    "aliases": ["验收日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间筛选验收记录。",
                },
                "accept_result": {
                    "description": "验收结果：passed 通过 / passed_with_defects 带缺陷通过。",
                    "aliases": ["验收结果", "验收结论"],
                    "semantic_role": "filter",
                    "value_range": "passed / passed_with_defects",
                    "business_usage": "筛选验收有问题的项目。",
                },
            },
        },
    }


@_with_units("mes_db")
def get_mes_business_meta() -> dict:
    """生产与工时域业务元数据。"""
    return {
        "work_order": {
            "description": "生产工单表，记录车间生产任务的工程号、车间、当前工序、状态、计划数量、排产与完工日期。",
            "aliases": ["生产工单表", "工单表", "排产表"],
            "columns": {
                "work_order_id": {
                    "description": "生产工单号。",
                    "aliases": ["工单号", "工单编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识工单，关联工时与质检表。",
                },
                "job_no": {
                    "description": "工程号，标明工单归属哪个工程。与 ERP 域同名同义。",
                    "aliases": ["工程号", "项目号", "工程编号"],
                    "semantic_role": "join_key",
                    "business_usage": "跨库关联到订单与项目。",
                    "keyword_text": "job_no 工程号 项目号 工程编号 工单归属",
                    "vector_text": "字段名：job_no。所属表：work_order。该字段表示工单归属的工程编号，可跨库关联订单与项目。",
                },
                "workshop": {
                    "description": "生产车间。",
                    "aliases": ["车间", "生产车间"],
                    "semantic_role": "dimension",
                    "value_range": "一车间 / 二车间",
                    "business_usage": "按车间统计工单与产能。",
                },
                "current_process": {
                    "description": "当前生产工序：开料 / 封边 / 排孔 / 组装 / 包装 / 已完工。",
                    "aliases": ["当前工序", "工序", "生产进度", "做到哪一步"],
                    "semantic_role": "output_dimension",
                    "value_range": "开料 / 封边 / 排孔 / 组装 / 包装 / 已完工",
                    "business_usage": "查询订单生产进度。",
                    "samples": ["开料", "封边", "排孔", "组装", "已完工"],
                    "keyword_text": "current_process 当前工序 工序 生产进度 开料 封边 排孔 组装 包装 已完工 工单表",
                    "vector_text": "字段名：current_process。所属表：work_order。该字段表示工单当前所处的生产工序，常作为查询结果输出。",
                    "rerank_text": "字段：work_order.current_process。含义：当前生产工序。别名：当前工序、生产进度。用途：询问订单做到哪道工序时作为输出字段。",
                },
                "work_status": {
                    "description": "工单状态：in_progress 在制 / finished 已完工。",
                    "aliases": ["工单状态", "生产状态"],
                    "semantic_role": "filter",
                    "value_range": "in_progress / finished",
                    "business_usage": "筛选在制或已完工工单。",
                },
                "plan_qty": {
                    "description": "计划生产数量（柜体件数）。",
                    "aliases": ["计划数量", "计划产量", "工单数量"],
                    "semantic_role": "metric",
                    "business_usage": "衡量工单规模。",
                },
                "schedule_date": {
                    "description": "排产日期。",
                    "aliases": ["排产日期", "排产时间", "开工日期"],
                    "semantic_role": "time",
                    "business_usage": "按排产时间筛选；识别已签单未排产的工程。",
                    "keyword_text": "schedule_date 排产日期 排产时间 开工日期 未排产",
                    "vector_text": "字段名：schedule_date。所属表：work_order。该字段表示工单的排产日期。",
                },
                "finish_date": {
                    "description": "完工日期，未完工为空。",
                    "aliases": ["完工日期", "完工时间"],
                    "semantic_role": "time",
                    "business_usage": "统计完工情况与生产周期。",
                },
            },
        },
        "labor_hour": {
            "description": "工时记录表，记录工人在各工序上投入的工时。",
            "aliases": ["工时表", "工时记录表", "报工表"],
            "columns": {
                "record_id": {
                    "description": "工时记录编号。",
                    "aliases": ["工时记录编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识工时记录。",
                },
                "work_order_id": {
                    "description": "所属工单号。",
                    "aliases": ["工单号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联工单表。",
                },
                "worker_name": {
                    "description": "工人姓名。",
                    "aliases": ["工人", "员工", "师傅", "工人姓名"],
                    "semantic_role": "dimension",
                    "business_usage": "按人统计工时。",
                },
                "process_name": {
                    "description": "工序名称。",
                    "aliases": ["工序", "工序名称"],
                    "semantic_role": "dimension",
                    "business_usage": "按工序统计工时。",
                },
                "hours": {
                    "description": "投入工时（小时）。",
                    "aliases": ["工时", "小时数", "耗时"],
                    "semantic_role": "metric",
                    "business_usage": "统计工时消耗、核算人工成本。",
                    "keyword_text": "hours 工时 小时数 耗时 人工 报工",
                    "vector_text": "字段名：hours。所属表：labor_hour。该字段表示工人在某道工序上投入的工时小时数。",
                    "rerank_text": "字段：labor_hour.hours。含义：投入工时。别名：工时、小时数。用途：统计工时消耗合计。",
                },
                "work_date": {
                    "description": "报工日期。",
                    "aliases": ["报工日期", "工作日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间统计工时。",
                },
            },
        },
        "capacity_stat": {
            "description": "产能统计表，按车间与统计日期记录计划产量与实际产量。",
            "aliases": ["产能表", "产能统计表"],
            "columns": {
                "capacity_id": {
                    "description": "产能记录编号。",
                    "aliases": ["产能记录编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识产能记录。",
                },
                "workshop": {
                    "description": "车间。",
                    "aliases": ["车间", "生产车间"],
                    "semantic_role": "dimension",
                    "business_usage": "按车间统计产能。",
                },
                "stat_date": {
                    "description": "统计日期（月末）。",
                    "aliases": ["统计日期", "统计月份"],
                    "semantic_role": "time",
                    "business_usage": "按月统计产能。",
                },
                "planned_qty": {
                    "description": "计划产量。",
                    "aliases": ["计划产量", "计划产能"],
                    "semantic_role": "metric",
                    "business_usage": "与实际产量比较算达成率。",
                },
                "actual_qty": {
                    "description": "实际产量。",
                    "aliases": ["实际产量", "实际产能", "完成产量"],
                    "semantic_role": "metric",
                    "business_usage": "与计划产量比较算达成率。",
                    "keyword_text": "actual_qty 实际产量 实际产能 完成产量 产能达成",
                    "vector_text": "字段名：actual_qty。所属表：capacity_stat。该字段表示车间实际完成的产量，与计划产量比较可算达成率。",
                },
            },
        },
        "quality_check": {
            "description": "质检表，记录工单的送检数量、合格数量与良率。",
            "aliases": ["质检表", "品质表", "良率表"],
            "columns": {
                "check_id": {
                    "description": "质检记录编号。",
                    "aliases": ["质检编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识质检记录。",
                },
                "work_order_id": {
                    "description": "所属工单号。",
                    "aliases": ["工单号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联工单表。",
                },
                "checked_qty": {
                    "description": "送检数量。",
                    "aliases": ["送检数量", "检验数量"],
                    "semantic_role": "metric",
                    "business_usage": "统计检验规模。",
                },
                "passed_qty": {
                    "description": "合格数量。",
                    "aliases": ["合格数量", "通过数量"],
                    "semantic_role": "metric",
                    "business_usage": "统计合格品数量。",
                },
                "yield_rate": {
                    "description": "良率，合格数量除以送检数量，0-1 之间。",
                    "aliases": ["良率", "合格率", "一次合格率", "良品率", "不良率"],
                    "semantic_role": "metric_filter",
                    "value_range": "0-1",
                    "business_usage": "筛选良率低的工单，定位质量问题。",
                    "samples": ["0.75", "0.905", "1.0"],
                    "keyword_text": "yield_rate 良率 合格率 一次合格率 质量 品质 低于",
                    "vector_text": "字段名：yield_rate。所属表：quality_check。该字段表示工单的良率（合格率），用于定位质量问题。",
                    "rerank_text": "字段：quality_check.yield_rate。含义：良率/合格率（0-1）。别名：良率、合格率。用途：筛选良率低于阈值的工单。",
                },
                "check_date": {
                    "description": "质检日期。",
                    "aliases": ["质检日期", "检验日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间统计质检。",
                },
            },
        },
    }


@_with_units("wms_db")
def get_wms_business_meta() -> dict:
    """库存与物料域业务元数据。"""
    return {
        "material": {
            "description": "物料主数据表，记录物料名称、品类、计量单位与安全库存。",
            "aliases": ["物料表", "材料表", "物料主数据"],
            "columns": {
                "material_code": {
                    "description": "物料编码。",
                    "aliases": ["物料编码", "material编码", "料号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识物料，关联批次与库存快照。",
                },
                "material_name": {
                    "description": "物料名称，如 18mm三聚氰胺颗粒板、烤漆门板、阻尼铰链。",
                    "aliases": ["物料", "物料名称", "材料名称", "材料", "料"],
                    "semantic_role": "output_dimension",
                    "samples": ["18mm三聚氰胺颗粒板", "烤漆门板", "阻尼铰链", "石英石台面"],
                    "keyword_text": "material_name 物料 物料名称 材料 材料名称 板材 门板 五金 台面",
                    "vector_text": "字段名：material_name。所属表：material。该字段表示物料的名称，常作为查询结果输出。",
                    "rerank_text": "字段：material.material_name。含义：物料名称。别名：物料、材料名称。用途：询问哪些材料时作为输出字段。",
                },
                "category": {
                    "description": "物料品类：板材 / 门板 / 五金 / 台面 / 辅料。",
                    "aliases": ["品类", "物料品类", "材料类别"],
                    "semantic_role": "dimension",
                    "value_range": "板材 / 门板 / 五金 / 台面 / 辅料",
                    "business_usage": "按品类分组统计。",
                },
                "unit": {
                    "description": "计量单位。",
                    "aliases": ["单位", "计量单位"],
                    "semantic_role": "dimension",
                    "business_usage": "展示物料单位。",
                },
                "safety_stock": {
                    "description": "安全库存量，低于此值需补货。",
                    "aliases": ["安全库存", "安全库存量", "最低库存", "补货点", "缺料"],
                    "semantic_role": "metric_filter",
                    "business_usage": "与在库数量比较判断是否需要补货。",
                    "keyword_text": "safety_stock 安全库存 安全库存量 最低库存 补货 缺货",
                    "vector_text": "字段名：safety_stock。所属表：material。该字段表示物料的安全库存量，在库数量低于它就需要补货。",
                    "rerank_text": "字段：material.safety_stock。含义：安全库存量。用途：与 stock_snapshot.on_hand_qty 比较判断是否低于安全库存。",
                },
            },
        },
        "batch": {
            "description": "批次表，记录物料入库批次的供应商、入库日期、入库数量与单位成本。",
            "aliases": ["批次表", "入库批次表"],
            "columns": {
                "batch_no": {
                    "description": "批次号。",
                    "aliases": ["批次号", "批号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识入库批次。",
                },
                "material_code": {
                    "description": "物料编码。",
                    "aliases": ["物料编码", "料号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联物料表。",
                },
                "supplier_id": {
                    "description": "供应商编号。与 ERP 域的 supplier_id 同域，跨库关联。",
                    "aliases": ["供应商ID", "供应商编号"],
                    "semantic_role": "join_key",
                    "business_usage": "跨库关联 ERP 的供应商表。",
                },
                "in_date": {
                    "description": "入库日期。",
                    "aliases": ["入库日期", "到货日期"],
                    "semantic_role": "time",
                    "business_usage": "按时间统计入库。",
                },
                "in_qty": {
                    "description": "入库数量。",
                    "aliases": ["入库数量", "到货数量"],
                    "semantic_role": "metric",
                    "business_usage": "统计入库量。",
                },
                "unit_cost": {
                    "description": "该批次的单位成本（敏感字段）。",
                    "aliases": ["单位成本", "成本单价", "进价"],
                    "semantic_role": "metric",
                    "business_usage": "核算材料成本。",
                    "keyword_text": "unit_cost 单位成本 成本单价 进价 采购价",
                    "vector_text": "字段名：unit_cost。所属表：batch。该字段表示该批次物料的单位成本。",
                },
            },
        },
        "stock_snapshot": {
            "description": "库存快照表，按日期记录物料的在库数量与锁定数量。",
            "aliases": ["库存表", "库存快照表", "结存表"],
            "columns": {
                "snapshot_id": {
                    "description": "快照记录编号。",
                    "aliases": ["快照编号"],
                    "semantic_role": "join_key",
                    "business_usage": "唯一标识快照记录。",
                },
                "material_code": {
                    "description": "物料编码。",
                    "aliases": ["物料编码", "料号"],
                    "semantic_role": "join_key",
                    "business_usage": "关联物料表。",
                },
                "snapshot_date": {
                    "description": "快照日期。",
                    "aliases": ["快照日期", "库存日期", "结存日期"],
                    "semantic_role": "time",
                    "business_usage": "按日期查询库存。",
                },
                "on_hand_qty": {
                    "description": "在库数量。",
                    "aliases": ["在库数量", "库存数量", "现有库存", "结存数量"],
                    "semantic_role": "metric_filter",
                    "business_usage": "查询库存水平；与安全库存比较判断缺货。",
                    "samples": ["18.0", "320.0", "1450.0"],
                    "keyword_text": "on_hand_qty 在库数量 库存数量 现有库存 结存数量 库存不足 缺货",
                    "vector_text": "字段名：on_hand_qty。所属表：stock_snapshot。该字段表示物料的在库数量，低于安全库存即需补货。",
                    "rerank_text": "字段：stock_snapshot.on_hand_qty。含义：在库数量。别名：库存数量、现有库存。用途：与 material.safety_stock 比较判断是否低于安全库存。",
                },
                "locked_qty": {
                    "description": "已锁定数量（已被工单占用）。",
                    "aliases": ["锁定数量", "占用数量"],
                    "semantic_role": "metric",
                    "business_usage": "计算可用库存 = 在库 - 锁定。",
                },
            },
        },
    }


def get_cross_database_links() -> list[dict]:
    """
    跨库关联键声明。

    四个库分属不同业务系统，**没有外键约束**，同一业务实体在不同库里
    命名还不一致。这些关联只能按命名约定推断，推错整条链路就错，而且
    错得静默——SQL 能跑通、有结果，只是结果是错的。

    把它显式声明出来注入 Prompt，比让模型从字段名自己猜可靠得多。
    confidence 标明证据强度：naming_convention 表示靠命名约定推断，
    不是数据库层面的外键。
    """
    return [
        {
            "left": "pm_db.project.project_code",
            "right": "erp_db.sales_order.job_no",
            "meaning": "项目编号与 ERP 工程号是同一个工程",
            "confidence": "naming_convention",
        },
        {
            "left": "pm_db.project.project_code",
            "right": "erp_db.purchase_order.job_no",
            "meaning": "项目编号与采购单的工程号是同一个工程",
            "confidence": "naming_convention",
        },
        {
            "left": "pm_db.project.project_code",
            "right": "mes_db.work_order.job_no",
            "meaning": "项目编号与生产工单的工程号是同一个工程",
            "confidence": "naming_convention",
        },
        {
            "left": "erp_db.supplier.supplier_id",
            "right": "wms_db.batch.supplier_id",
            "meaning": "ERP 供应商编号与入库批次的供应商编号同域",
            "confidence": "naming_convention",
        },
    ]


def get_metric_disambiguation() -> list[dict]:
    """
    跨库同名/近义指标的口径区分。

    典型错误：用户问「采购总金额」，模型留在项目库用了 contract_amt（合同额），
    而正确答案在 ERP 的 po_amt（采购额）。两者都是「金额」，字段名都合理，
    模型只看 Schema 无从判断，必须显式写死口径。
    """
    return [
        {
            "term": "采购金额 / 采购总额 / 采购成本",
            "correct": "erp_db.purchase_order.po_amt（含税）",
            "wrong": "pm_db.project.contract_amt（合同额，不含税，不是采购）",
        },
        {
            "term": "合同额 / 合同金额",
            "correct": "pm_db.project.contract_amt（不含税）",
            "wrong": "erp_db.sales_order.order_amt（订单金额，含税，口径不同）",
        },
        {
            "term": "订单金额 / 签单金额",
            "correct": "erp_db.sales_order.order_amt（含税）",
            "wrong": "pm_db.project.contract_amt（合同额，不含税）",
        },
    ]


def get_database_catalog() -> list[dict]:
    """
    数据库目录元数据，供三级检索的库级（Tier 1）路由使用。

    四个库分属不同业务系统，彼此无外键约束，跨库查询需应用层分步合并。
    """
    return [
        {
            "name": "erp_db",
            "description": "ERP 系统：订单与采购域。记录销售订单、采购单、供应商与付款，覆盖含税订单金额、采购金额、供应商准时率、付款状态等。",
            "aliases": ["ERP", "ERP库", "订单库", "采购库"],
        },
        {
            "name": "pm_db",
            "description": "项目管理系统：项目与工期域。记录项目、里程碑、变更单与验收，覆盖不含税合同额、预算、实际成本、工期节点、延期状态等。",
            "aliases": ["项目库", "项目管理库", "工期库"],
        },
        {
            "name": "mes_db",
            "description": "生产系统：生产与工时域。记录生产工单、工时报工、产能统计与质检，覆盖生产工序、排产日期、工时、产量、良率等。",
            "aliases": ["生产库", "MES", "车间库", "工单库"],
        },
        {
            "name": "wms_db",
            "description": "仓储系统：库存与物料域。记录物料主数据、入库批次与库存快照，覆盖物料品类、安全库存、在库数量、批次成本等。",
            "aliases": ["库存库", "仓储库", "WMS", "物料库"],
        },
    ]


def get_metric_definitions() -> dict:
    """
    业务指标定义：这些词到底怎么算。

    与 get_metric_disambiguation 的分工：那边回答「该用哪个字段」（给检索和生成用），
    这里回答「这个说法是什么意思」（给对话问答用）。同一个词两边都要有——
    模型算得对，还得能跟用户解释清楚口径，否则用户没法判断数字可不可信。

    以前这份定义硬编码在 RuleBasedDataQAClient 里，只有演示库能用；
    现在它是语义层的一部分，接真实库时写进 semantic/_shared.yaml 的
    metric_definitions 即可。
    """
    return {
        "超预算": "超预算指项目实际成本已超过立项时批准的预算金额（actual_cost > budget_amt）。",
        "合同额": "合同额是与甲方签订的不含税合同总价，与 ERP 中含税的订单金额口径不同。",
        "准时率": "准时率是供应商按约定交期到货的批次占比，用于评估供应商交付表现。",
        "良率": "良率是质检合格数量与送检数量之比，反映工单的一次交检合格水平。",
        "安全库存": "安全库存是为应对需求波动设定的最低库存量，在库数量低于它即需补货。",
    }
