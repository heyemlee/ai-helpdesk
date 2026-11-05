"""The approved nine-table, sixty-column business schema and its labels."""
from __future__ import annotations

from dataclasses import dataclass


ORDER_STATUSES = ("已签单", "生产中", "待发货", "配送中", "已交付", "已取消")
PRODUCTION_STAGES = ("裁切", "封边", "钻孔", "组装", "包装")
PRODUCTION_STATUSES = ("待生产", "生产中", "已完工")
DELIVERY_METHODS = ("自送", "物流", "客户自提")
DELIVERY_STATUSES = ("待配送", "配送中", "已送达")
ISSUE_TYPES = ("缺件", "破损", "质量问题", "安装问题", "发错货")
SERVICE_STATUSES = ("待处理", "处理中", "已解决")


def choices(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


@dataclass(frozen=True)
class Column:
    name: str
    label: str
    label_en: str
    ddl: str
    role: str = "text"
    usage: str = ""
    unit: str = ""
    values: tuple[str, ...] = ()


def status(name: str, label: str, label_en: str, values: tuple[str, ...]) -> Column:
    return Column(name, label, label_en,
                  f"TEXT NOT NULL CHECK ({name} IN ({choices(values)}))",
                  "filter", values=values)


def order_key(primary: bool = False) -> Column:
    return Column("order_no", "订单号", "Order number",
                  "TEXT NOT NULL" + (" PRIMARY KEY" if primary else ""),
                  "join_key", "四个数据库统一通过 order_no 关联订单，不使用工程号。")


@dataclass(frozen=True)
class Table:
    label: str
    label_en: str
    description: str
    columns: tuple[Column, ...]
    constraints: tuple[str, ...] = ()


ERP_TABLES = {
    "orders": Table("订单", "Orders", "订单主表：客户联系信息、合同金额、签单与交付日期、订单状态及负责销售。", (
        order_key(primary=True),
        Column("customer_name", "客户姓名", "Customer name", "TEXT NOT NULL", "filter"),
        Column("phone", "手机号", "Customer phone", "TEXT", usage="按文本保存，支持国家区号。原始模拟数据没有号码，迁移后为空。"),
        Column("contract_amount", "合同金额", "Contract amount", "REAL NOT NULL CHECK (contract_amount >= 0)", "metric",
               "美元、不含税的合同总额。明细小计之和不一定等于谈定的合同金额；没有回款记录，不能计算未付余额。", "USD"),
        Column("signed_date", "签单日期", "Signed date", "TEXT NOT NULL", "time"),
        Column("delivery_date", "交付日期", "Delivery date", "TEXT", "time",
               "迁移自原配送记录的计划送货日期。没有送货计划时为空；不是安装日期。实际送达日期在 svc_db.delivery.delivered_date。"),
        status("order_status", "订单状态", "Order status", ORDER_STATUSES),
        Column("address", "地址", "Delivery address", "TEXT NOT NULL", usage="完整交付地址，包含街道、城市和州。"),
        Column("order_notes", "订单备注", "Order notes", "TEXT"),
        Column("salesperson", "销售人员", "Salesperson", "TEXT", "filter", "负责该订单的销售；原模拟数据没有此信息，迁移后为空。"),
    )),
    "order_item": Table("订单明细", "Order items", "每行是一张订单的一项产品；同一订单可以有多行相同型号。", (
        order_key(),
        Column("product_name", "产品名称", "Product name", "TEXT NOT NULL", "filter", "产品名称与规格，例如地柜、吊柜、台面或五金。"),
        Column("model", "型号", "Model", "TEXT NOT NULL", "filter"),
        Column("quantity", "数量", "Quantity", "INTEGER NOT NULL CHECK (quantity > 0)", "metric"),
        Column("unit_price", "单价", "Unit price", "REAL NOT NULL CHECK (unit_price >= 0)", "metric", unit="USD"),
        Column("subtotal", "小计", "Subtotal", "REAL NOT NULL DEFAULT 0", "metric",
               "数量 × 单价，保留两位小数，数据库在插入或更新时自动计算。", "USD"),
    ), ("FOREIGN KEY (order_no) REFERENCES orders(order_no)",)),
}

PROD_TABLES = {
    "work_order": Table("生产记录", "Production records", "每张订单的当前工序、生产状态、加急标记及生产备注；不记录生产日期和负责人。", (
        order_key(primary=True),
        status("current_stage", "当前工序", "Current stage", PRODUCTION_STAGES),
        status("production_status", "生产状态", "Production status", PRODUCTION_STATUSES),
        Column("is_rush", "是否加急", "Rush order", "INTEGER NOT NULL CHECK (is_rush IN (0, 1))", "filter", "1 表示加急，0 表示普通订单。"),
        Column("production_notes", "生产备注", "Production notes", "TEXT", usage="特殊要求、生产问题、暂停原因；暂停订单的生产状态仍归为生产中。"),
    )),
}

WMS_TABLES = {
    "material": Table("物料与库存", "Materials and inventory", "通过物料名称识别材料，包含规格、库存及库位。无物料编号和单位成本。", (
        Column("material_name", "物料名称", "Material name", "TEXT NOT NULL PRIMARY KEY", "join_key", "名称包含规格。同名、同计量单位的原物料合并库存。"),
        Column("uom", "计量单位", "Unit of measure", "TEXT NOT NULL", "dimension", "库存和到货数量按物料自己的单位计量，不同单位不可直接相加。"),
        Column("on_hand_qty", "现有库存", "On-hand quantity", "INTEGER NOT NULL CHECK (on_hand_qty >= 0)", "metric"),
        Column("allocated_qty", "已占用库存", "Allocated quantity", "INTEGER NOT NULL CHECK (allocated_qty >= 0)", "metric"),
        Column("safety_stock", "安全库存", "Safety stock", "INTEGER NOT NULL CHECK (safety_stock >= 0)", "metric"),
        Column("location", "存放位置", "Storage location", "TEXT NOT NULL", "filter", "同名物料存放于多个库位时，列出所有库位。"),
        Column("material_notes", "物料备注", "Material notes", "TEXT"),
    )),
    "supplier": Table("供应商", "Suppliers", "供应商名称及联系信息，通过供应商名称关联到货记录，无供应商编号。", (
        Column("supplier_name", "供应商名称", "Supplier name", "TEXT NOT NULL PRIMARY KEY", "join_key"),
        Column("contact_name", "联系人", "Contact name", "TEXT"),
        Column("phone", "联系电话", "Contact phone", "TEXT", usage="按文本保存，支持国家区号。"),
        Column("address", "地址", "Supplier address", "TEXT"),
        Column("supplier_notes", "供应商备注", "Supplier notes", "TEXT"),
    )),
    "receipt": Table("到货记录", "Material receipts", "通过物料名称和供应商名称记录采购及实际收货；不记录预计到货日期。", (
        Column("material_name", "物料名称", "Material name", "TEXT NOT NULL", "join_key"),
        Column("supplier_name", "供应商名称", "Supplier name", "TEXT NOT NULL", "join_key"),
        Column("purchased_qty", "采购数量", "Purchased quantity", "INTEGER NOT NULL CHECK (purchased_qty > 0)", "metric"),
        Column("received_qty", "已到货数量", "Received quantity", "INTEGER NOT NULL CHECK (received_qty >= 0 AND received_qty <= purchased_qty)", "metric"),
        Column("received_date", "实际到货日期", "Actual receipt date", "TEXT", "time", "未到货时为空。多次收货时代表最近一次收货日期。"),
        Column("receipt_notes", "到货备注", "Receipt notes", "TEXT"),
    ), ("FOREIGN KEY (material_name) REFERENCES material(material_name)",
        "FOREIGN KEY (supplier_name) REFERENCES supplier(supplier_name)")),
}

SVC_TABLES = {
    "delivery": Table("配送记录", "Deliveries", "配送方式、配送状态和实际送达日期；客户姓名、手机号、地址从 ERP 订单获取。", (
        order_key(),
        status("delivery_method", "配送方式", "Delivery method", DELIVERY_METHODS),
        status("delivery_status", "配送状态", "Delivery status", DELIVERY_STATUSES),
        Column("delivery_person", "配送人员", "Delivery person", "TEXT"),
        Column("phone", "联系电话", "Delivery phone", "TEXT"),
        Column("delivered_date", "实际送达日期", "Actual delivery date", "TEXT", "time", "尚未送达时为空。"),
        Column("delivery_notes", "配送备注", "Delivery notes", "TEXT"),
    )),
    "service_ticket": Table("售后工单", "Service tickets", "订单问题、报修日期、处理状态与解决日期。每张订单可以有多条售后记录。", (
        order_key(),
        status("issue_type", "问题类型", "Issue type", ISSUE_TYPES),
        Column("description", "问题描述", "Issue description", "TEXT NOT NULL"),
        Column("reported_date", "报修日期", "Reported date", "TEXT NOT NULL", "time"),
        status("service_status", "处理状态", "Service status", SERVICE_STATUSES),
        Column("resolved_date", "解决日期", "Resolved date", "TEXT", "time", "尚未解决时为空。"),
        Column("service_notes", "售后备注", "Service notes", "TEXT"),
    )),
    "replacement_part": Table("补件记录", "Replacement parts", "按订单记录补件名称、数量、申请日期、发出日期和处理状态。", (
        order_key(),
        Column("part_name", "补件名称", "Part name", "TEXT NOT NULL", "join_key", "补件名称包含规格，通过名称对应仓储物料。"),
        Column("quantity", "补件数量", "Part quantity", "INTEGER NOT NULL CHECK (quantity > 0)", "metric"),
        Column("requested_date", "申请日期", "Requested date", "TEXT NOT NULL", "time"),
        status("part_status", "补件状态", "Part status", SERVICE_STATUSES),
        Column("shipped_date", "发出日期", "Shipped date", "TEXT", "time", "已发出不代表已解决；原已发出但未收到的补件归为处理中。"),
        Column("part_notes", "补件备注", "Part notes", "TEXT", usage="记录补件原因和配送说明。通过订单号只能关联订单，不能唯一定位同一订单的某张售后工单。"),
    )),
}

DATABASES = {
    "erp_db": ("erp.db", "订单与客户", "Orders and customers", ERP_TABLES),
    "prod_db": ("prod.db", "生产进度", "Production progress", PROD_TABLES),
    "wms_db": ("wms.db", "物料与库存", "Materials and inventory", WMS_TABLES),
    "svc_db": ("svc.db", "配送与售后", "Delivery and after-sales", SVC_TABLES),
}


def create_schema(conn, tables: dict[str, Table]) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    for name, table in tables.items():
        definitions = [f'"{c.name}" {c.ddl}' for c in table.columns]
        definitions.extend(table.constraints)
        conn.execute(f'CREATE TABLE "{name}" ({", ".join(definitions)})')
        for column in table.columns:
            if column.role in ("join_key", "time", "filter") and "PRIMARY KEY" not in column.ddl:
                conn.execute(f'CREATE INDEX "idx_{name}_{column.name}" ON "{name}" ("{column.name}")')
    if "order_item" in tables:
        conn.executescript("""
            CREATE TRIGGER order_item_subtotal_insert AFTER INSERT ON order_item
            BEGIN
                UPDATE order_item SET subtotal = ROUND(NEW.quantity * NEW.unit_price, 2)
                WHERE rowid = NEW.rowid;
            END;
            CREATE TRIGGER order_item_subtotal_update
            AFTER UPDATE OF quantity, unit_price, subtotal ON order_item
            WHEN NEW.subtotal != ROUND(NEW.quantity * NEW.unit_price, 2)
            BEGIN
                UPDATE order_item SET subtotal = ROUND(NEW.quantity * NEW.unit_price, 2)
                WHERE rowid = NEW.rowid;
            END;
        """)
