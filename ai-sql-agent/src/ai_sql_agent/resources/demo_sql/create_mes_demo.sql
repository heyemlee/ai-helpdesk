-- 生产系统：生产与工时域
-- job_no 与 ERP 域同名同义（都是工程号），与项目管理域的 project_code 是同一实体。
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS quality_check;
DROP TABLE IF EXISTS labor_hour;
DROP TABLE IF EXISTS capacity_stat;
DROP TABLE IF EXISTS work_order;

CREATE TABLE work_order (
    work_order_id   TEXT PRIMARY KEY,
    job_no          TEXT NOT NULL,
    workshop        TEXT NOT NULL,
    current_process TEXT NOT NULL,
    work_status     TEXT NOT NULL,
    plan_qty        INTEGER NOT NULL,
    schedule_date   TEXT NOT NULL,
    finish_date     TEXT
);

CREATE TABLE labor_hour (
    record_id     TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL,
    worker_name   TEXT NOT NULL,
    process_name  TEXT NOT NULL,
    hours         REAL NOT NULL,
    work_date     TEXT NOT NULL,
    FOREIGN KEY (work_order_id) REFERENCES work_order(work_order_id)
);

CREATE TABLE capacity_stat (
    capacity_id  TEXT PRIMARY KEY,
    workshop     TEXT NOT NULL,
    stat_date    TEXT NOT NULL,
    planned_qty  INTEGER NOT NULL,
    actual_qty   INTEGER NOT NULL
);

CREATE TABLE quality_check (
    check_id      TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL,
    checked_qty   INTEGER NOT NULL,
    passed_qty    INTEGER NOT NULL,
    yield_rate    REAL NOT NULL,
    check_date    TEXT NOT NULL,
    FOREIGN KEY (work_order_id) REFERENCES work_order(work_order_id)
);

INSERT INTO work_order VALUES
    ('WO-3001','J24001','一车间','封边',  'in_progress',120,'2024-02-01',NULL),
    ('WO-3002','J24001','二车间','组装',  'in_progress', 80,'2024-03-18',NULL),
    ('WO-3003','J24002','二车间','已完工','finished',    15,'2024-02-15','2024-03-10'),
    ('WO-3004','J24003','一车间','排孔',  'in_progress',200,'2024-04-02',NULL),
    ('WO-3005','J24003','二车间','开料',  'in_progress',180,'2024-05-06',NULL),
    ('WO-3006','J24004','一车间','组装',  'in_progress',  8,'2024-03-25',NULL),
    ('WO-3007','J24005','一车间','已完工','finished',     3,'2024-03-05','2024-03-20'),
    ('WO-3008','J24006','一车间','开料',  'in_progress',  5,'2024-04-25',NULL),
    ('WO-3009','J24008','一车间','已完工','finished',     4,'2024-04-05','2024-04-18');

INSERT INTO labor_hour VALUES
    ('LH-001','WO-3001','张建国','开料',38.5,'2024-02-05'),
    ('LH-002','WO-3001','李伟',  '封边',42.0,'2024-02-20'),
    ('LH-003','WO-3001','王强',  '封边',36.5,'2024-03-01'),
    ('LH-004','WO-3002','刘敏',  '组装',28.0,'2024-03-22'),
    ('LH-005','WO-3003','李伟',  '组装',12.5,'2024-02-28'),
    ('LH-006','WO-3003','王强',  '包装', 6.0,'2024-03-08'),
    ('LH-007','WO-3004','张建国','开料',88.0,'2024-04-08'),
    ('LH-008','WO-3004','李伟',  '排孔',76.5,'2024-04-25'),
    ('LH-009','WO-3005','刘敏',  '开料',54.0,'2024-05-10'),
    ('LH-010','WO-3006','王强',  '组装', 9.5,'2024-04-01'),
    ('LH-011','WO-3007','张建国','组装', 5.0,'2024-03-12'),
    ('LH-012','WO-3009','李伟',  '组装', 6.5,'2024-04-10');

INSERT INTO capacity_stat VALUES
    ('CAP-01','一车间','2024-02-29',300,268),
    ('CAP-02','二车间','2024-02-29',200,195),
    ('CAP-03','一车间','2024-03-31',320,301),
    ('CAP-04','二车间','2024-03-31',200,172),
    ('CAP-05','一车间','2024-04-30',350,289),
    ('CAP-06','二车间','2024-04-30',220,208);

INSERT INTO quality_check VALUES
    ('QC-001','WO-3001',120,113,0.9417,'2024-03-02'),
    ('QC-002','WO-3003', 15, 15,1.0000,'2024-03-10'),
    ('QC-003','WO-3004',200,181,0.9050,'2024-04-28'),
    ('QC-004','WO-3006',  8,  8,1.0000,'2024-04-05'),
    ('QC-005','WO-3007',  3,  3,1.0000,'2024-03-20'),
    ('QC-006','WO-3009',  4,  3,0.7500,'2024-04-18');
