-- 项目管理：项目与工期域
-- project_code 与 ERP 域的 job_no 指同一工程，命名不同且无外键。
-- contract_amt 为不含税合同额，与 ERP 的 po_amt/order_amt（含税）口径不同。
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS acceptance;
DROP TABLE IF EXISTS change_order;
DROP TABLE IF EXISTS milestone;
DROP TABLE IF EXISTS project;

CREATE TABLE project (
    project_code   TEXT PRIMARY KEY,
    project_name   TEXT NOT NULL,
    business_unit  TEXT NOT NULL,
    contract_amt   REAL NOT NULL,   -- 不含税
    budget_amt     REAL NOT NULL,
    actual_cost    REAL NOT NULL,
    start_date     TEXT NOT NULL,
    due_date       TEXT NOT NULL,
    project_status TEXT NOT NULL
);

CREATE TABLE milestone (
    milestone_id   TEXT PRIMARY KEY,
    project_code   TEXT NOT NULL,
    milestone_name TEXT NOT NULL,
    plan_date      TEXT NOT NULL,
    actual_date    TEXT,
    ms_status      TEXT NOT NULL,
    FOREIGN KEY (project_code) REFERENCES project(project_code)
);

CREATE TABLE change_order (
    change_id     TEXT PRIMARY KEY,
    project_code  TEXT NOT NULL,
    change_amt    REAL NOT NULL,
    change_reason TEXT NOT NULL,
    approved_date TEXT,
    FOREIGN KEY (project_code) REFERENCES project(project_code)
);

CREATE TABLE acceptance (
    acceptance_id TEXT PRIMARY KEY,
    project_code  TEXT NOT NULL,
    accept_date   TEXT NOT NULL,
    accept_result TEXT NOT NULL,
    FOREIGN KEY (project_code) REFERENCES project(project_code)
);

INSERT INTO project VALUES
    ('J24001','万科精装1号楼橱柜工程','建筑装修',1250000.00,1100000.00,1180000.00,'2024-01-20','2024-06-30','active'),
    ('J24002','绿城样板房全屋定制',  '建筑装修', 380000.00, 340000.00, 322000.00,'2024-02-11','2024-04-30','completed'),
    ('J24003','城建安置房橱柜批量',  '建筑装修',2100000.00,1900000.00,2050000.00,'2024-03-28','2024-09-30','active'),
    ('J24004','李先生别墅木作',      '建筑装修', 152000.00, 130000.00, 128000.00,'2024-03-15','2024-05-30','active'),
    ('J24005','陈女士住宅橱柜',      '建筑装修',  86000.00,  75000.00,  74000.00,'2024-03-02','2024-04-20','completed'),
    ('J24006','周先生公寓衣柜',      '建筑装修',  98000.00,  88000.00,  91000.00,'2024-04-20','2024-06-10','active'),
    ('J24007','园区消防管道改造',    '消防',     680000.00, 620000.00, 598000.00,'2024-04-05','2024-08-15','active'),
    ('J24008','孙女士住宅橱柜',      '建筑装修', 134000.00, 120000.00, 118000.00,'2024-04-01','2024-05-25','completed');

INSERT INTO milestone VALUES
    ('MS-001','J24001','深化图纸确认','2024-02-10','2024-02-14','delayed'),
    ('MS-002','J24001','首批柜体进场','2024-04-15','2024-04-15','on_time'),
    ('MS-003','J24001','整体验收',    '2024-06-30',NULL,        'pending'),
    ('MS-004','J24002','深化图纸确认','2024-02-20','2024-02-18','on_time'),
    ('MS-005','J24002','整体验收',    '2024-04-30','2024-04-28','on_time'),
    ('MS-006','J24003','深化图纸确认','2024-04-10','2024-04-22','delayed'),
    ('MS-007','J24003','首批柜体进场','2024-06-01',NULL,        'pending'),
    ('MS-008','J24004','整体验收',    '2024-05-30',NULL,        'pending'),
    ('MS-009','J24005','整体验收',    '2024-04-20','2024-04-19','on_time'),
    ('MS-010','J24007','管道预埋完成','2024-06-01',NULL,        'pending'),
    ('MS-011','J24008','整体验收',    '2024-05-25','2024-05-24','on_time');

INSERT INTO change_order VALUES
    ('CO-001','J24001', 62000.00,'客户增加中岛柜','2024-03-05'),
    ('CO-002','J24001', 18000.00,'门板材质升级',  '2024-04-02'),
    ('CO-003','J24003',145000.00,'户型调整重做',  '2024-05-10'),
    ('CO-004','J24003', 32000.00,'五金品牌变更',  NULL),
    ('CO-005','J24006',  9000.00,'增加顶柜',      '2024-05-06');

INSERT INTO acceptance VALUES
    ('AC-001','J24002','2024-04-28','passed'),
    ('AC-002','J24005','2024-04-19','passed'),
    ('AC-003','J24008','2024-05-24','passed_with_defects');
