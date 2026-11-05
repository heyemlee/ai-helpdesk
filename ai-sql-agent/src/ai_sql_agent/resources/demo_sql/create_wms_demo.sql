-- 仓储：库存与物料域
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS stock_snapshot;
DROP TABLE IF EXISTS batch;
DROP TABLE IF EXISTS material;

CREATE TABLE material (
    material_code TEXT PRIMARY KEY,
    material_name TEXT NOT NULL,
    category      TEXT NOT NULL,
    unit          TEXT NOT NULL,
    safety_stock  INTEGER NOT NULL
);

CREATE TABLE batch (
    batch_no      TEXT PRIMARY KEY,
    material_code TEXT NOT NULL,
    supplier_id   TEXT NOT NULL,
    in_date       TEXT NOT NULL,
    in_qty        REAL NOT NULL,
    unit_cost     REAL NOT NULL,
    FOREIGN KEY (material_code) REFERENCES material(material_code)
);

CREATE TABLE stock_snapshot (
    snapshot_id   TEXT PRIMARY KEY,
    material_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    on_hand_qty   REAL NOT NULL,
    locked_qty    REAL NOT NULL,
    FOREIGN KEY (material_code) REFERENCES material(material_code)
);

INSERT INTO material VALUES
    ('MAT-001','18mm三聚氰胺颗粒板','板材',  '张',200),
    ('MAT-002','烤漆门板',          '门板',  '平米',80),
    ('MAT-003','实木门板',          '门板',  '平米',40),
    ('MAT-004','双饰面门板',        '门板',  '平米',120),
    ('MAT-005','阻尼铰链',          '五金',  '只',1000),
    ('MAT-006','静音滑轨',          '五金',  '套',500),
    ('MAT-007','石英石台面',        '台面',  '延米',60),
    ('MAT-008','环保封边条',        '辅料',  '米',2000);

INSERT INTO batch VALUES
    ('B-24020101','MAT-001','SUP-01','2024-02-01',600,185.00),
    ('B-24022001','MAT-001','SUP-01','2024-02-20',400,182.00),
    ('B-24040101','MAT-001','SUP-01','2024-04-01',900,178.00),
    ('B-24020501','MAT-002','SUP-04','2024-02-05',150,320.00),
    ('B-24031001','MAT-003','SUP-04','2024-03-10', 60,680.00),
    ('B-24040501','MAT-004','SUP-01','2024-04-05',400,210.00),
    ('B-24012601','MAT-005','SUP-02','2024-01-26',3000,8.50),
    ('B-24040201','MAT-006','SUP-02','2024-04-02',1200,12.00),
    ('B-24020201','MAT-007','SUP-03','2024-02-02', 90,880.00),
    ('B-24011501','MAT-008','SUP-01','2024-01-15',5000,2.30);

INSERT INTO stock_snapshot VALUES
    ('SS-0401','MAT-001','2024-04-30',  320.0, 180.0),
    ('SS-0402','MAT-002','2024-04-30',   62.0,  40.0),
    ('SS-0403','MAT-003','2024-04-30',   18.0,  12.0),
    ('SS-0404','MAT-004','2024-04-30',  260.0, 200.0),
    ('SS-0405','MAT-005','2024-04-30', 1450.0, 800.0),
    ('SS-0406','MAT-006','2024-04-30',  380.0, 260.0),
    ('SS-0407','MAT-007','2024-04-30',   34.0,  20.0),
    ('SS-0408','MAT-008','2024-04-30', 3100.0,1200.0);
