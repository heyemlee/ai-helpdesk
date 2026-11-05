"""
Emit the semantic layer for the simulated ERP.

**Why this file exists and why it is not hand-written YAML.** The semantic layer is
what the model uses to recognise the business: aliases decide whether a colloquial
question can retrieve a column at all, description decides whether the model reads
it correctly, semantic_role decides whether it gets used as a filter, a metric or a
join key. Sixteen tables come to roughly 120 columns, and each column needs three
retrieval strings on top of its prose (keyword_text for BM25, vector_text for dense
retrieval, rerank_text for the reranker). Those three are formulaic -- writing them
by hand 120 times guarantees they drift out of sync with the prose above them.

So the prose is authored once per column in COLUMNS below, and the three retrieval
strings are derived. Editing a description updates all three.

**Aliases are bilingual on purpose.** The helpdesk that consumes this serves both
Chinese and English speakers (its console ships both), and the probe that motivated
this dataset included an English phrasing. A column only reachable through Chinese
aliases is invisible to half the traffic.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

# (column, description, aliases, semantic_role, business_usage)
# semantic_role: join_key | filter | metric | attribute | time
Col = tuple[str, str, list[str], str, str]

ERP_TABLES: dict[str, tuple[str, list[str], list[Col]]] = {
    "customer": (
        "客户主数据。建筑商（production builder）、总包（GC）、经销商与零售业主四类客户。",
        ["客户表", "客户主数据", "customer", "builder", "客户"],
        [
            ("customer_id", "客户编号。", ["客户号", "客户编号", "customer id"], "join_key", "唯一标识客户。"),
            ("customer_name", "客户名称，建筑商公司名、总包名或业主姓名。",
             ["客户名", "客户名称", "公司名", "builder name", "customer name"], "filter",
             "按名字找客户时用。注意存在近似重名（如 Marlowe Homes 与 Marlowe Homes LLC），"
             "精确匹配会漏，模糊匹配会多。"),
            ("customer_type", "客户类型：production_builder 量产建筑商 / general_contractor 总包 / dealer 经销商 / retail 零售业主。",
             ["客户类型", "客户性质", "customer type"], "filter",
             "四类客户的付款条件与工程类型完全不同，按类型分组才有意义。"),
            ("payment_terms", "付款条件：net_30 / net_45 / net_60 月结天数，deposit_50 为零售的五成定金。",
             ["付款条件", "账期", "payment terms", "net30"], "dimension",
             "决定发票的到期日（due_date = 开票日 + 账期天数）。"),
            ("credit_limit", "信用额度（美元）。零售客户没有授信，该字段为 NULL。",
             ["信用额度", "授信", "credit limit"], "metric",
             "**零售客户为 NULL 不是 0**：AVG/SUM 会跳过它们，`credit_limit > x` 也筛不到。"),
            ("is_active", "是否活跃客户，1 活跃 0 停用。",
             ["是否活跃", "活跃", "停用", "active"], "filter",
             "停用客户仍留在库里。问「我们有多少客户」时通常要 is_active = 1。"),
        ],
    ),
    "job": (
        "工程（job）主表。一个 job 对应一套橱柜的完整交付，量产房是一栋一个 job，商业工程是一个标段一个 job。",
        ["工程表", "工程", "订单表", "job", "jobs", "工单表"],
        [
            ("job_no", "工程号，形如 J25-00123。全公司唯一，也是生产、送货、售后三个系统回指本表的键。",
             ["工程号", "工程编号", "job号", "job number", "job no", "订单号"], "join_key",
             "跨库关联键：prod_db / svc_db 都按命名约定用 job_no 指回这里，**没有外键约束**，"
             "关联错了 SQL 照样跑通、结果照样有行，只是行是错的。"),
            ("customer_id", "所属客户编号。", ["客户号", "customer id"], "join_key", "关联 customer 表。"),
            ("subdivision", "小区名（builder 的楼盘）。商业工程没有小区，该字段为 NULL。",
             ["小区", "楼盘", "subdivision"], "filter",
             "**商业工程为 NULL**，按小区筛选会把整个商业业务筛没。"),
            ("lot_no", "地块号（lot）。商业工程为 NULL。",
             ["地块号", "lot", "lot number", "门牌"], "filter",
             "**lot 号在不同小区之间会重复**，单独一个 lot 42 不能唯一定位工程，必须配合 subdivision。"),
            ("plan_name", "户型名（builder 的标准户型，如 Plan 1685）。商业工程为 NULL。",
             ["户型", "图号", "plan", "plan name"], "filter", "量产房按户型批量下单时用。"),
            ("address", "施工地址。", ["地址", "工地地址", "address"], "text", "送货与安装用地址。"),
            ("city", "城市。", ["城市", "city"], "filter", "按区域统计时用。"),
            ("state", "州的原始录入值（两位缩写）。**大小写不统一，不要用它做筛选**。",
             ["州原始值", "state raw"], "dimension",
             "只用于原样展示。绝大多数行是 CA/NV，但约 3% 是手工录入的 Ca/Nv，"
             "`state = 'CA'` 会静默漏掉它们（862 vs 893）。**按州筛选一律用 state_norm**。"),
            ("state_norm", "规范化后的州（大写去空格），按州筛选、分组一律用这一列。",
             ["州", "state", "哪个州", "加州", "内华达"], "filter",
             "生成列，等于 UPPER(TRIM(state))。它存在的理由就是给按州的查询一个无歧义的目标——"
             "原始 state 列大小写不统一，靠每条查询自己记得加 UPPER() 不可靠。"),
            ("job_type", "工程类型：production 量产房 / semi_custom 半定制 / commercial 商业 / service 售后服务单。",
             ["工程类型", "订单类型", "job type"], "filter", "四类的金额量级和交付流程都不同。"),
            ("contract_amount", "合同金额（美元，**不含税**）。变更单的增减不在这个数里。",
             ["合同额", "合同金额", "订单金额", "contract amount", "金额"], "metric",
             "**口径**：不含税，且不含变更单。工程实际总额 = contract_amount + 该工程已批准变更单的 SUM(amount_delta)。"),
            ("sold_date", "签单日期。", ["签单日", "下单日期", "sold date"], "time", "按月/季统计签单量用这个日期。"),
            ("target_install_date", "计划安装日期。工程还没下达生产（sold）或已取消时为 NULL。",
             ["计划安装日", "交期", "target date", "install date"], "time",
             "**未下达和已取消的工程为 NULL**，按交期排序会把它们排到一头。"),
            ("job_status", "工程状态：sold 已签未下达 / released 已下达 / in_production 生产中 / shipped 已发货 / installed 已安装 / closed 已结案 / cancelled 已取消。",
             ["工程状态", "订单状态", "job status", "状态"], "filter",
             "**sold 和 cancelled 的工程没有生产工单**，和 prod_db 做内连接会静默丢掉约 14% 的工程。"),
        ],
    ),
    "job_item": (
        "工程明细行。一个工程下的每一项柜体、台面、五金、线条。",
        ["明细", "订单明细", "行项目", "job item", "line item", "配置单"],
        [
            ("item_id", "明细行编号。", ["明细号", "item id"], "join_key", "唯一标识明细行。"),
            ("job_no", "所属工程号。", ["工程号", "job number"], "join_key", "关联 job 表。"),
            ("line_no", "行号，工程内顺序。", ["行号", "line"], "dimension", "同一工程内的排序。"),
            ("category", "品类：base_cabinet 地柜 / wall_cabinet 吊柜 / tall_cabinet 高柜 / vanity 浴室柜 / countertop 台面 / hardware 五金 / molding 线条 / accessory 配件。",
             ["品类", "类别", "category", "柜型"], "filter", "按品类统计用量或金额时用。"),
            ("sku", "物料编码。", ["物料号", "编码", "sku"], "join_key", "对应 wms_db.material.sku。"),
            ("description", "规格描述，含尺寸。", ["规格", "描述", "description"], "text", "给人看的规格文字。"),
            ("qty", "数量。", ["数量", "qty", "件数"], "metric", "行数量。"),
            ("unit_price", "单价（美元，不含税）。", ["单价", "unit price"], "metric", "单件价格。"),
            ("extended_price", "行金额 = 单价 × 数量。", ["行金额", "小计", "extended price"], "metric",
             "**行金额合计不等于 contract_amount**：合同额是谈下来的总价，明细是配置清单，两者有折扣与打包差异。"),
        ],
    ),
    "change_order": (
        "变更单。签约后对配置、尺寸、饰面的改动，或退让给 builder 的折让。",
        ["变更单", "改单", "change order", "CO", "加减项"],
        [
            ("change_order_no", "变更单号。", ["变更单号", "CO号", "change order no"], "join_key", "唯一标识变更单。"),
            ("job_no", "所属工程号。", ["工程号", "job number"], "join_key", "关联 job 表。"),
            ("reason", "变更原因：door_style_change 换门型 / added_cabinets 加柜 / size_change 改尺寸 / finish_change 改饰面 / builder_credit 折让给建筑商 / field_conflict 现场冲突。",
             ["变更原因", "原因", "reason"], "filter", "按原因归类分析改单来源。"),
            ("amount_delta", "金额增减（美元，不含税）。**折让为负数**。",
             ["变更金额", "增减", "加价", "amount delta"], "metric",
             "**有正有负**：builder_credit 是负数。求变更总额必须 SUM(amount_delta)，"
             "按绝对值求和或只取正数都会算错。"),
            ("requested_date", "提出日期。", ["提出日", "申请日期", "requested date"], "time", "变更提出的时间。"),
            ("approved_date", "批准日期。待审时为 NULL。", ["批准日", "approved date"], "time",
             "**待审为 NULL**，统计审批时长必须排除它们。"),
            ("co_status", "状态：pending 待审 / approved 已批 / rejected 已驳回。",
             ["变更状态", "审批状态", "co status"], "filter",
             "**算工程金额时只计入 approved**，把 pending 和 rejected 算进去会高估。"
             "但这只管「这个工程实际多少钱」这类问题——**用户问「所有变更单」「一共提过多少变更」"
             "时指的就是全部，不要自作主张加 co_status 过滤**。加不加这个条件取决于问题问的是"
             "「生效的金额」还是「提过的单子」。"),
        ],
    ),
    "invoice": (
        "应收发票。按定金 / 进度款 / 尾款三段开票。",
        ["发票", "应收", "账单", "invoice", "开票"],
        [
            ("invoice_no", "发票号。", ["发票号", "invoice no"], "join_key", "唯一标识发票。"),
            ("job_no", "所属工程号。", ["工程号", "job number"], "join_key", "关联 job 表。"),
            ("invoice_type", "类型：deposit 定金 / progress 进度款 / final 尾款。",
             ["发票类型", "款项类型", "invoice type", "定金", "尾款"], "filter",
             "问「尾款」时指的是 invoice_type = 'final' 这一张。"),
            ("invoice_date", "开票日期。", ["开票日", "invoice date"], "time", "开票时间。"),
            ("amount", "开票金额（美元，不含税）。", ["开票金额", "发票金额", "amount"], "metric", "本张发票的应收额。"),
            ("due_date", "到期日 = 开票日 + 客户账期。", ["到期日", "due date", "账期到期"], "time",
             "**逾期判据**：due_date < 今天 且 invoice_status 不是 paid。"),
            ("invoice_status", "状态：open 未收 / partial 部分收款 / paid 已收清 / void 作废。",
             ["发票状态", "收款状态", "invoice status"], "filter",
             "**partial 是最容易漏的一档**：它既不是未付也不是已付，未付余额要用发票金额减去实收合计。"
             "**void 是作废发票，不构成应收**：统计逾期、欠款、应收账款时必须排除它——"
             "写 `invoice_status != 'paid'` 会把作废的算进去，正确写法是 "
             "`invoice_status IN ('open','partial')`。未收清 = open 或 partial，不含 void。"),
        ],
    ),
    "payment_received": (
        "客户回款。一张发票可能分多笔收。",
        ["回款", "收款", "客户付款", "payment", "到账"],
        [
            ("payment_id", "收款流水号。", ["流水号", "payment id"], "join_key", "唯一标识一笔收款。"),
            ("invoice_no", "对应发票号。", ["发票号", "invoice no"], "join_key",
             "关联 invoice 表。**未付余额 = invoice.amount − 该发票下 payment_received.amount 的合计**，"
             "只看发票行读不出余额。"),
            ("amount", "本笔收款金额（美元）。", ["收款金额", "到账金额", "amount"], "metric", "单笔到账额。"),
            ("received_date", "到账日期。", ["到账日", "收款日期", "received date"], "time",
             "**逾期回款判据**：received_date > 对应发票的 due_date。"),
            ("method", "收款方式：check 支票 / ach / credit_card 信用卡 / wire 电汇。",
             ["收款方式", "付款方式", "method"], "dimension", "到账渠道。"),
        ],
    ),
}

PROD_TABLES: dict[str, tuple[str, list[str], list[Col]]] = {
    "work_order": (
        "生产工单。工程下达车间后一个工程开一张工单，记录当前做到哪道工序。",
        ["工单", "生产工单", "work order", "WO", "生产单"],
        [
            ("wo_no", "工单号。", ["工单号", "wo number", "work order no"], "join_key", "唯一标识工单。"),
            ("job_no", "对应工程号。", ["工程号", "job number"], "join_key",
             "**跨库关联键**：按命名约定指向 erp_db.job.job_no，没有外键。这是「我的柜子做到哪了」"
             "这类问题必须走的一跳。"),
            ("plant", "生产厂区：Plant 1 / Plant 2。", ["厂区", "车间", "plant"], "filter", "按厂区分组产能。"),
            ("current_stage", "当前工序：cut 开料 / edgeband 封边 / drill 打孔 / assembly 组装 / finish 喷涂 / qc 质检 / pack 打包 / done 完成。",
             ["工序", "当前工序", "做到哪一步", "stage", "current stage"], "filter",
             "客户问「做到哪一步了」，答案就是这个字段。"),
            ("wo_status", "工单状态：scheduled 已排产 / in_progress 生产中 / on_hold 挂起 / complete 完成 / cancelled 取消。",
             ["工单状态", "生产状态", "wo status"], "filter", "在制品是 in_progress 与 on_hold。"),
            ("priority", "优先级：normal 普通 / rush 加急。", ["优先级", "加急", "priority", "rush"], "filter", "加急单排产优先。"),
            ("scheduled_date", "排产日期。", ["排产日", "计划日期", "scheduled date"], "time", "计划进入生产的日期。"),
            ("completed_date", "完工日期。未完工时为 NULL。", ["完工日", "完成日期", "completed date"], "time",
             "**在制工单为 NULL**：按完工日期过滤会把所有正在做的单子排除掉，"
             "这正是「有多少在制」和「这个月完工多少」必须分开写的原因。"),
            ("hold_reason", "挂起原因，仅 on_hold 时有值，其余为 NULL。",
             ["挂起原因", "卡住原因", "hold reason", "为什么停了"], "text",
             "客户问「为什么我的单子卡住了」，只有挂起的单子答得上来。"),
        ],
    ),
    "stage_log": (
        "工序流水。工单每经过一道工序记一条，含起止时间、操作工与设备。",
        ["工序流水", "报工", "工序记录", "stage log"],
        [
            ("log_id", "流水号。", ["流水号", "log id"], "join_key", "唯一标识一条工序记录。"),
            ("wo_no", "工单号。", ["工单号", "wo number"], "join_key", "关联 work_order 表。"),
            ("stage", "工序名。", ["工序", "stage"], "filter", "该条记录属于哪道工序。"),
            ("started_at", "开工时间。", ["开工时间", "started at"], "time", "进入该工序的时间。"),
            ("finished_at", "完工时间。当前正在做的那道工序为 NULL。",
             ["完工时间", "finished at"], "time",
             "**正在进行的工序为 NULL**，算工序耗时必须排除，否则会把在制的算成零耗时。"),
            ("operator", "操作工。", ["操作工", "工人", "operator"], "dimension", "谁做的。"),
            ("machine", "设备编号。组装、打包等手工站为 NULL。", ["设备", "机台", "machine"], "dimension",
             "**手工站为 NULL**，按设备统计会漏掉手工工序。"),
        ],
    ),
    "qc_check": (
        "质检记录。工单过质检时记一条，含合格数、不合格数与缺陷代码。",
        ["质检", "检验", "qc", "quality check"],
        [
            ("check_id", "质检单号。", ["质检号", "check id"], "join_key", "唯一标识质检记录。"),
            ("wo_no", "工单号。", ["工单号", "wo number"], "join_key", "关联 work_order 表。"),
            ("checked_date", "质检日期。", ["质检日", "checked date"], "time", "检验时间。"),
            ("inspector", "质检员。", ["质检员", "检验员", "inspector"], "dimension", "谁检的。"),
            ("passed_qty", "合格数量。", ["合格数", "passed"], "metric", "通过检验的件数。"),
            ("failed_qty", "不合格数量。", ["不合格数", "failed", "报废数"], "metric", "未通过的件数。"),
            ("defect_code", "缺陷代码。全部合格时为 NULL。", ["缺陷代码", "缺陷", "defect code"], "filter",
             "**全合格为 NULL**：COUNT(defect_code) 与 COUNT(*) 不相等，这正是「有多少批次出过问题」的算法。"),
        ],
    ),
}

WMS_TABLES: dict[str, tuple[str, list[str], list[Col]]] = {
    "supplier": (
        "供应商主数据。门板、箱体、五金、台面、油漆五类。",
        ["供应商", "厂商", "supplier", "vendor"],
        [
            ("supplier_id", "供应商编号。", ["供应商号", "supplier id"], "join_key", "唯一标识供应商。"),
            ("supplier_name", "供应商名称。", ["供应商名", "厂商名", "supplier name"], "filter", "按名称找供应商。"),
            ("category", "供应品类：door 门板 / box 箱体 / hardware 五金 / countertop 台面 / finish 油漆。",
             ["品类", "供应类别", "category"], "filter", "按供应品类分组。"),
            ("on_time_rate", "准时到货率，0 到 1。", ["准时率", "准交率", "on time rate"], "metric",
             "按约定到货日准时送达的批次占比，用来评估供应商。"),
            ("lead_time_days", "标准交货周期（天）。", ["交期", "货期", "lead time"], "metric", "下单到到货的标准天数。"),
        ],
    ),
    "material": (
        "物料主数据。",
        ["物料", "材料", "material", "SKU"],
        [
            ("sku", "物料编码。", ["物料号", "编码", "sku"], "join_key", "唯一标识物料，也是 job_item 与补件请求的关联键。"),
            ("description", "物料描述。", ["物料名", "描述", "description"], "text", "给人看的物料说明。"),
            ("category", "物料品类。", ["品类", "category"], "filter", "按品类归类。"),
            ("uom", "计量单位：each 件 / sheet 张 / lf 延尺 / sqft 平方英尺。", ["单位", "计量单位", "uom"], "dimension",
             "**单位不统一**：跨品类求和数量没有意义，必须按 uom 分组。"),
            ("safety_stock", "安全库存量。", ["安全库存", "safety stock"], "metric", "低于它就该补货。"),
            ("unit_cost", "标准成本（美元）。", ["成本", "单位成本", "unit cost"], "metric", "物料成本单价。"),
        ],
    ),
    "receipt": (
        "到货记录。供应商每批来料记一条。",
        ["到货", "收货", "入库", "receipt"],
        [
            ("receipt_id", "到货单号。", ["到货单号", "receipt id"], "join_key", "唯一标识一批到货。"),
            ("sku", "物料编码。", ["物料号", "sku"], "join_key", "关联 material 表。"),
            ("supplier_id", "供应商编号。", ["供应商号", "supplier id"], "join_key", "关联 supplier 表。"),
            ("qty", "到货数量。", ["数量", "qty"], "metric", "本批件数。"),
            ("promised_date", "承诺到货日。", ["承诺交期", "promised date"], "time", "供应商答应的到货日期。"),
            ("received_date", "实际到货日。尚未到货时为 NULL。", ["实际到货日", "received date"], "time",
             "**未到货为 NULL**：只比较 received_date > promised_date 会漏掉「压根没来」的那批，"
             "而那批恰恰是最严重的迟到。"),
            ("lot_code", "批次号。", ["批次", "lot", "lot code"], "dimension", "追溯用批次编码。"),
        ],
    ),
    "inventory": (
        "库存快照。每个物料一行当前在库与已分配数量。",
        ["库存", "在库", "inventory", "现存量"],
        [
            ("sku", "物料编码。", ["物料号", "sku"], "join_key", "关联 material 表。"),
            ("on_hand_qty", "在库数量。", ["在库", "现存量", "on hand"], "metric", "仓库里实际有多少。"),
            ("allocated_qty", "已分配数量（已被工单占用）。", ["已分配", "占用", "allocated"], "metric",
             "**可用量 = on_hand_qty − allocated_qty**，只看在库会高估可用。"),
            ("location", "库位。", ["库位", "货位", "location"], "dimension", "存放位置。"),
            ("last_counted", "最近盘点日期。从未盘点过的为 NULL。", ["盘点日期", "last counted"], "time",
             "**从未盘点过的为 NULL**，这些物料的账面数最不可信。"),
        ],
    ),
}

SVC_TABLES: dict[str, tuple[str, list[str], list[Col]]] = {
    "delivery": (
        "送货记录。工程完工后送到工地。",
        ["送货", "发货", "配送", "delivery", "shipping"],
        [
            ("delivery_no", "送货单号。", ["送货单号", "delivery no"], "join_key", "唯一标识一次送货。"),
            ("job_no", "对应工程号。", ["工程号", "job number"], "join_key",
             "**跨库关联键**：按命名约定指向 erp_db.job.job_no。"),
            ("scheduled_date", "计划送货日。", ["计划送货日", "预计送货", "scheduled date"], "time",
             "客户问「什么时候送货」，没送的看这个字段。"),
            ("delivered_date", "实际送达日。未送达时为 NULL。", ["实际送达日", "delivered date"], "time",
             "**逾期判据**：delivered_date IS NULL 且 scheduled_date < 今天，就是该到没到。"),
            ("carrier", "承运方式：own_truck 自有车队 / ltl_freight 零担物流 / customer_pickup 客户自提。",
             ["承运", "运输方式", "carrier"], "filter", "谁送的。"),
            ("delivery_status", "状态：scheduled 已排期 / in_transit 在途 / delivered 已送达 / failed 送货失败 / rescheduled 已改期。",
             ["送货状态", "delivery status"], "filter",
             "**failed 与 rescheduled 都还没送到**，只按 delivered 反查会把它们错算成在途。"),
            ("address", "送货地址。", ["送货地址", "address"], "text", "送到哪。"),
            ("notes", "备注，仅异常送货有值。", ["备注", "notes"], "text", "送货失败或改期的原因写在这里。"),
        ],
    ),
    "service_ticket": (
        "售后服务单。缺件、破损、瑕疵、安装问题、错发。",
        ["售后单", "服务单", "工单", "投诉", "service ticket", "ticket"],
        [
            ("ticket_no", "服务单号。", ["服务单号", "ticket no"], "join_key", "唯一标识服务单。"),
            ("job_no", "对应工程号。", ["工程号", "job number"], "join_key", "**跨库关联键**：指向 erp_db.job.job_no。"),
            ("issue_type", "问题类型：missing_part 缺件 / damage 破损 / defect 瑕疵 / install_issue 安装问题 / wrong_item 错发。",
             ["问题类型", "issue type", "缺件", "破损"], "filter",
             "客户说「少了两个铰链」对应的就是 missing_part。"),
            ("reported_date", "报修日期。", ["报修日", "reported date"], "time", "客户提出的时间。"),
            ("resolved_date", "解决日期。未结时为 NULL。", ["解决日期", "resolved date"], "time",
             "**未结为 NULL**：算平均处理时长时不排除它们，结果一定偏小。"),
            ("ticket_status", "状态：open 新建 / in_progress 处理中 / waiting_parts 等件 / resolved 已解决 / closed 已关闭。",
             ["服务单状态", "ticket status"], "filter",
             "waiting_parts 表示在等补件到货。"
             "**resolved 和 closed 都是已经处理完的**，区别只是有没有走完关闭流程，"
             "两者都有 resolved_date。所以「未结 / 还没处理完 / 挂着的」指的是 "
             "open、in_progress、waiting_parts 这三档，判据用 `resolved_date IS NULL` 最稳——"
             "写 `ticket_status <> 'closed'` 会把已解决的也算成未结。"),
            ("description", "问题描述。", ["描述", "问题说明", "description"], "text", "客户反映的具体情况。"),
            ("reported_by", "报修人：builder superintendent 工地主管 / installer 安装工 / homeowner 业主。",
             ["报修人", "reported by"], "filter", "谁报的。"),
        ],
    ),
    "replacement_part": (
        "补件请求。服务单确认要补的件，跟踪到发出为止。",
        ["补件", "配件补发", "replacement part", "补发"],
        [
            ("request_id", "补件请求号。", ["补件号", "request id"], "join_key", "唯一标识补件请求。"),
            ("ticket_no", "对应服务单号。", ["服务单号", "ticket no"], "join_key", "关联 service_ticket 表。"),
            ("sku", "物料编码。", ["物料号", "sku"], "join_key", "对应 wms_db.material.sku。"),
            ("qty", "补件数量。", ["数量", "qty"], "metric", "补几件。"),
            ("requested_date", "申请日期。", ["申请日", "requested date"], "time", "提出补件的时间。"),
            ("shipped_date", "发出日期。未发出时为 NULL。", ["发出日期", "shipped date"], "time",
             "**未发出为 NULL**，客户问「我的补件寄了吗」看的就是这个字段有没有值。"),
            ("request_status", "状态：requested 已申请 / in_production 生产中 / shipped 已发出 / received 已签收 / cancelled 已取消。",
             ["补件状态", "request status"], "filter", "补件走到哪一步了。"),
        ],
    ),
}


DATABASES = {
    "erp_db": ("erp.db", ERP_TABLES,
               "ERP 系统：工程、明细、变更单、应收发票与客户回款。合同金额不含税。"
               "客户分建筑商 / 总包 / 经销商 / 零售四类。",
               ["ERP", "ERP库", "订单库", "工程库", "应收库", "erp"]),
    "prod_db": ("prod.db", PROD_TABLES,
                "生产系统（MES）：生产工单、工序流水与质检。回答「做到哪一步了」「为什么卡住」。",
                ["生产库", "MES", "车间库", "工单库", "production"]),
    "wms_db": ("wms.db", WMS_TABLES,
               "仓储系统：供应商、物料主数据、到货记录与库存快照。",
               ["库存库", "仓储库", "WMS", "物料库", "warehouse"]),
    "svc_db": ("svc.db", SVC_TABLES,
               "交付与售后系统：送货记录、售后服务单与补件请求。"
               "回答「什么时候送货」「少件怎么补」「补件寄了吗」。",
               ["售后库", "服务库", "送货库", "service", "delivery"]),
}


def _samples(conn, table: str, column: str, limit: int = 3) -> list[str]:
    """真实取样，而不是手写。手写的样例只要数据一变就成了谎话。"""
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL ORDER BY {column} LIMIT {limit}"
        ).fetchall()
    except Exception:
        return []
    out = []
    for (v,) in rows:
        text = str(v)
        if len(text) <= 40:          # 长描述当样例没有意义，只会挤占 Token
            out.append(text)
    return out


# 所有 time 角色的字段统一带上这句。
#
# 压测里抓到的：问「有多少张发票逾期了」，模型写 `due_date < DATE('now')`，逻辑
# 完全正确，但 SQLite 的 DATE('now') 返回 **UTC**。加州本地下午五点之后 UTC 已经
# 是第二天，于是「今天」整体偏一天——每天最后三分之一个工作日，所有「今天到期」
# 「本周要送」「这个月签了多少」的结果都是错的，而且错得看不出来：SQL 跑通、
# 有结果、数字只差一点点。
#
# 库里存的全是本地业务日期（签单日、到期日、排产日），所以比较基准也必须是本地。
_LOCALTIME_NOTE = (
    "【与「今天」比较时】用 DATE('now','localtime')，不要用 DATE('now')——"
    "后者是 UTC，本地傍晚之后会滚到第二天，「今天到期/本周/本月」会整体偏一天。"
    "本库存的是本地业务日期。"
)


# 契约 §4.6.5：字段单位。
#
# 这里**不是新造事实**——单位本来就写在上面每一列的 description 里
# （「合同金额（美元，不含税）」「标准交货周期（天）」）。写在散文里模型看得懂、
# 程序读不出来，于是服务端不下发币种，调用方只能在自己那边配一个全局
# `SQL_AGENT_CURRENCY` 顶上；那个常量在多币种部署下必然错，而且是静默的。
# 这张表做的事就是把已经写下来的单位提成机器可读的一列。
#
# 键是 `库.表.列`，值是单位标识：ISO 4217 币种大写，其余用短标识。
# **拿不准的列一律不写**——契约 §4.6.5 说得很清楚，缺失是「单位未知」，
# 调用方不得擅自补；猜一个填进去比留空危险得多。
COLUMN_UNITS: dict[str, str] = {
    # ── 金额：sim_erp 模拟的是美国橱柜厂，描述里逐条写着「美元」 ──
    "erp_db.customer.credit_limit": "USD",
    "erp_db.job.contract_amount": "USD",
    "erp_db.job_item.unit_price": "USD",
    "erp_db.job_item.extended_price": "USD",
    "erp_db.change_order.amount_delta": "USD",
    "erp_db.invoice.amount": "USD",
    "erp_db.payment_received.amount": "USD",
    "wms_db.material.unit_cost": "USD",
    # ── 件数：uom 不统一的 material.* 除外（描述里明说「跨品类求和没有意义」）──
    "erp_db.job_item.qty": "EA",
    "prod_db.qc_check.passed_qty": "EA",
    "prod_db.qc_check.failed_qty": "EA",
    "wms_db.receipt.qty": "EA",
    "wms_db.inventory.on_hand_qty": "EA",
    "wms_db.inventory.allocated_qty": "EA",
    "svc_db.replacement_part.qty": "EA",
    # ── 时长 ──
    "wms_db.supplier.lead_time_days": "day",
}


def _column_entry(table: str, col: Col, samples: list[str], database: str = "") -> dict:
    name, desc, aliases, role, usage = col
    if role == "time":
        usage = f"{usage} {_LOCALTIME_NOTE}"
    alias_line = " ".join(aliases)
    entry = {
        "description": desc,
        "aliases": aliases,
        "semantic_role": role,
        "business_usage": usage,
        # 三段检索文本按同一份 prose 派生，改描述三段一起变，不会各说各话。
        "keyword_text": f"{name} {alias_line} {table}",
        "vector_text": f"字段名：{name}。所属表：{table}。{desc}",
        "rerank_text": f"字段：{table}.{name}。含义：{desc} 角色：{role}。",
    }
    if samples:
        entry["samples"] = samples
    unit = COLUMN_UNITS.get(f"{database}.{table}.{name}")
    if unit:
        entry["unit"] = unit
    return entry


def _assert_units_apply(database: str, tables: dict) -> None:
    """
    COLUMN_UNITS 里指向这个库的键必须都命中真实字段。

    键写错的后果是**静默不生效**：单位没下发，调用方回退到它自己那个全局币种
    常量，金额照样报出去、看起来完全正常。这类错误不会有人发现，所以在生成
    元数据的时候就得炸掉。
    """
    known = {
        f"{database}.{table}.{column}"
        for table, meta in tables.items()
        for column in meta["columns"]
    }
    stale = sorted(
        key
        for key in COLUMN_UNITS
        if key.startswith(f"{database}.") and key not in known
    )
    if stale:
        raise ValueError(
            f"COLUMN_UNITS 里这些键在 {database} 里找不到对应字段（写错了会静默不生效）："
            + "、".join(stale)
        )


def _primary_key(conn, table: str) -> list[str]:
    """读表的真实主键。没有显式主键的表返回空列表。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return []
    keyed = [(row[5], row[1]) for row in rows if row[5]]
    return [name for _position, name in sorted(keyed)]


def build_semantic(sim_dir: Path, out_dir: Path) -> list[str]:
    import sqlite3

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for db_name, (filename, tables, db_desc, db_aliases) in DATABASES.items():
        db_path = sim_dir / filename
        conn = sqlite3.connect(str(db_path))
        doc = {
            "name": db_name,
            "description": db_desc,
            "aliases": db_aliases,
            "connection": {"dialect": "sqlite", "db_path": str(db_path)},
            "tables": {},
        }
        for table, (tdesc, taliases, cols) in tables.items():
            entry = {
                "description": tdesc,
                "aliases": taliases,
                "columns": {
                    c[0]: _column_entry(table, c, _samples(conn, table, c[0]), db_name)
                    for c in cols
                },
            }
            # 契约 §4.6.6 的 key_columns：直接读库里真实的主键定义，不靠猜。
            primary_key = _primary_key(conn, table)
            if primary_key:
                entry["primary_key"] = primary_key
            doc["tables"][table] = entry
        conn.close()
        _assert_units_apply(db_name, doc["tables"])
        path = out_dir / f"{db_name}.yaml"
        path.write_text(
            "# 模拟 ERP 的语义元数据 —— 由 tools/sim_erp/semantic.py 生成，不要手改。\n"
            "# 改业务描述请改 tools/sim_erp/semantic.py 里的规格表，再重新生成。\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        written.append(path.name)

    shared = {
        "cross_database_links": [
            {"left": "erp_db.job.job_no", "right": "prod_db.work_order.job_no",
             "meaning": "工程号与生产工单的工程号是同一个工程", "confidence": "naming_convention"},
            {"left": "erp_db.job.job_no", "right": "svc_db.delivery.job_no",
             "meaning": "工程号与送货单的工程号是同一个工程", "confidence": "naming_convention"},
            {"left": "erp_db.job.job_no", "right": "svc_db.service_ticket.job_no",
             "meaning": "工程号与售后服务单的工程号是同一个工程", "confidence": "naming_convention"},
            {"left": "erp_db.job_item.sku", "right": "wms_db.material.sku",
             "meaning": "明细行的物料编码与物料主数据同域", "confidence": "naming_convention"},
            {"left": "svc_db.replacement_part.sku", "right": "wms_db.material.sku",
             "meaning": "补件的物料编码与物料主数据同域", "confidence": "naming_convention"},
        ],
        "metric_disambiguation": [
            {"term": "合同额 / 合同金额 / 订单金额",
             "correct": "erp_db.job.contract_amount",
             "wrong": "erp_db.job_item.extended_price（配置清单行金额，合计与谈定的合同额有折扣差）"},
            {"term": "工程总额 / 最终金额（含变更）",
             "correct": "erp_db.change_order.amount_delta",
             "wrong": "erp_db.job.contract_amount（只有它会漏掉变更单增减；见 metric_definitions 的算法）"},
            {"term": "还欠多少 / 还有多少钱没付 / 未付总额",
             "correct": "erp_db.job.contract_amount",
             "wrong": "erp_db.invoice.amount（只汇总已开票的发票会漏掉尚未开票的尾款——"
                      "尾款常常没开，客户会以为快付清了。算法见 metric_definitions 的「工程未付总额」）"},
            {"term": "当前到期应付 / 这张发票还差多少",
             "correct": "erp_db.payment_received.amount",
             "wrong": "erp_db.invoice.invoice_status（状态只有 open/partial/paid，读不出还差多少钱）"},
            {"term": "可用库存 / 能不能发货",
             "correct": "wms_db.inventory.allocated_qty",
             "wrong": "wms_db.inventory.on_hand_qty（单看在库会高估，已被工单占用的发不了）"},
            {"term": "交期 / 什么时候能送到",
             "correct": "svc_db.delivery.scheduled_date",
             "wrong": "erp_db.job.target_install_date（计划安装日不是送货日，两者差着物流那一段）"},
        ],
        "metric_definitions": {
            "逾期发票": "开票已过到期日（due_date < 今天）且仍未收清（invoice_status 不是 paid）的发票。",
            "单张发票未付余额": "**一张发票**的应收减去该发票下所有回款之和，"
                        "即 invoice.amount − SUM(payment_received.amount)。只管这一张，不代表整个工程。",
            "当前到期应付": "该工程**已开出**的所有发票金额合计，减去已收回款合计。"
                       "尾款未开票时它会明显小于工程欠款总额。",
            "工程未付总额": "整个工程还欠多少：合同额 + 已批准变更单增减 − 已收回款合计。"
                       "**客户问「还有多少钱没付」「还欠多少」通常指的是这个**，"
                       "因为它包含了尚未开票的尾款。只报「当前到期应付」会让客户以为快付清了。",
            "在制": "生产工单状态为 in_progress 或 on_hold，即已下达车间但尚未完工。",
            "未收清发票": "invoice_status 为 open 或 partial 的发票。**不含 void**——"
                     "作废发票不构成应收，算逾期或欠款时把它算进去会虚高。",
            "未结服务单": "resolved_date 为空的服务单，即 open / in_progress / waiting_parts 三档。"
                     "resolved 和 closed 都算已处理完，只是关闭流程走没走完。",
            "逾期送货": "计划送货日已过（scheduled_date < 今天）但实际送达日仍为空的送货单。",
            "准时率": "供应商按承诺日期到货的批次占比，用于评估供应商交付表现。",
            "可用库存": "在库数量减去已被工单占用的数量，即 on_hand_qty − allocated_qty。",
            "安全库存": "为应对需求波动设定的最低库存量，在库低于它即需补货。",
            "良率": "质检合格数量占送检总量之比，passed_qty / (passed_qty + failed_qty)。",
        },
    }
    path = out_dir / "_shared.yaml"
    path.write_text(
        "# 跨库共享语义 —— 由 tools/sim_erp/semantic.py 生成，不要手改。\n"
        + yaml.safe_dump(shared, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    written.append(path.name)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Emit the semantic layer for the simulated ERP.")
    ap.add_argument("--sim", default="runtime_data/sim", help="simulated database directory")
    ap.add_argument("--out", default="tools/sim_erp/semantic", help="semantic YAML output directory")
    args = ap.parse_args()
    written = build_semantic(Path(args.sim), Path(args.out))
    for name in written:
        print(f"  {name}")
    print(f"\n  -> {args.out}")


if __name__ == "__main__":
    main()
