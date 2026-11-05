-- ERP：订单与采购域
-- 注意两个刻意保留的 ERP 遗留命名：job_no（工程号）、po_amt（采购金额）
-- job_no 与项目管理域的 project_code 是同一业务实体，但两库命名不同，
-- 无外键约束，只能按命名约定推断关联——这是跨库准确率低于单库的主因之一。
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS payment;
DROP TABLE IF EXISTS purchase_order;
DROP TABLE IF EXISTS supplier;
DROP TABLE IF EXISTS sales_order;

CREATE TABLE sales_order (
    order_no      TEXT PRIMARY KEY,
    job_no        TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    order_amt     REAL NOT NULL,   -- 含税
    sign_date     TEXT NOT NULL,
    order_status  TEXT NOT NULL
);

CREATE TABLE supplier (
    supplier_id   TEXT PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    category      TEXT NOT NULL,
    on_time_rate  REAL NOT NULL
);

CREATE TABLE purchase_order (
    po_no       TEXT PRIMARY KEY,
    job_no      TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    po_amt      REAL NOT NULL,      -- 含税，与 pm_db.project.contract_amt（不含税）口径不同
    po_date     TEXT NOT NULL,
    po_status   TEXT NOT NULL,
    FOREIGN KEY (supplier_id) REFERENCES supplier(supplier_id)
);

CREATE TABLE payment (
    payment_id TEXT PRIMARY KEY,
    po_no      TEXT NOT NULL,
    paid_amt   REAL NOT NULL,
    paid_date  TEXT NOT NULL,
    pay_status TEXT NOT NULL,
    FOREIGN KEY (po_no) REFERENCES purchase_order(po_no)
);

INSERT INTO sales_order VALUES
    ('SO-2024-001','J24001','万科精装1号楼',1412500.00,'2024-01-20','in_production'),
    ('SO-2024-002','J24002','绿城样板房',   429400.00,'2024-02-11','delivered'),
    ('SO-2024-003','J24003','城建安置房',  2373000.00,'2024-03-28','in_production'),
    ('SO-2024-004','J24004','李先生别墅',   171760.00,'2024-03-15','in_production'),
    ('SO-2024-005','J24005','陈女士住宅',    97180.00,'2024-03-02','delivered'),
    ('SO-2024-006','J24006','周先生公寓',   110740.00,'2024-04-20','in_production'),
    ('SO-2024-007','J24007','消防改造项目',  680000.00,'2024-04-05','signed'),
    ('SO-2024-008','J24008','孙女士住宅',   151420.00,'2024-04-01','delivered');

INSERT INTO supplier VALUES
    ('SUP-01','华鹏板材','板材',0.95),
    ('SUP-02','东顺五金','五金',0.88),
    ('SUP-03','杰森石英石','台面',0.72),
    ('SUP-04','立信涂料','涂料',0.91),
    ('SUP-05','广辉玻璃','玻璃',0.65);

INSERT INTO purchase_order VALUES
    ('PO-24-0101','J24001','SUP-01',386000.00,'2024-01-25','received'),
    ('PO-24-0102','J24001','SUP-02', 92000.00,'2024-01-26','received'),
    ('PO-24-0103','J24001','SUP-03',158000.00,'2024-02-02','received'),
    ('PO-24-0201','J24002','SUP-01',104000.00,'2024-02-15','received'),
    ('PO-24-0202','J24002','SUP-04', 36000.00,'2024-02-16','received'),
    ('PO-24-0301','J24003','SUP-01',742000.00,'2024-04-01','partial'),
    ('PO-24-0302','J24003','SUP-02',186000.00,'2024-04-02','received'),
    ('PO-24-0303','J24003','SUP-05', 88000.00,'2024-04-10','ordered'),
    ('PO-24-0401','J24004','SUP-01', 48000.00,'2024-03-20','received'),
    ('PO-24-0501','J24005','SUP-01', 26000.00,'2024-03-05','received'),
    ('PO-24-0601','J24006','SUP-02', 31000.00,'2024-04-22','ordered'),
    ('PO-24-0801','J24008','SUP-01', 42000.00,'2024-04-03','received');

INSERT INTO payment VALUES
    ('PAY-0001','PO-24-0101',386000.00,'2024-02-10','paid'),
    ('PAY-0002','PO-24-0102', 92000.00,'2024-02-11','paid'),
    ('PAY-0003','PO-24-0103', 79000.00,'2024-02-20','partial'),
    ('PAY-0004','PO-24-0201',104000.00,'2024-03-01','paid'),
    ('PAY-0005','PO-24-0202', 36000.00,'2024-03-02','paid'),
    ('PAY-0006','PO-24-0301',371000.00,'2024-04-20','partial'),
    ('PAY-0007','PO-24-0302',186000.00,'2024-04-25','paid'),
    ('PAY-0008','PO-24-0401', 48000.00,'2024-04-01','paid'),
    ('PAY-0009','PO-24-0501', 26000.00,'2024-03-20','paid'),
    ('PAY-0010','PO-24-0801', 42000.00,'2024-04-20','paid');
