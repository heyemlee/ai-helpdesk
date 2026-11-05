-- Simulated ERP: orders and receivables (US cabinet shop serving production builders + GCs)
--
-- Naming is deliberately business-plain (job_no, contract_amount) rather than
-- ERP-legacy (FBILLNO). A real Kingdee/SAP extract would NOT look like this --
-- that gap is the single biggest piece of work when a real ERP is connected,
-- and it is called out in sim_erp/README.md so nobody mistakes this for a
-- rehearsal of *that* problem.
--
-- No foreign keys ACROSS databases (they are separate systems, same as production).
-- Within a database FKs are declared, because a real ERP would have them.
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS payment_received;
DROP TABLE IF EXISTS invoice;
DROP TABLE IF EXISTS change_order;
DROP TABLE IF EXISTS job_item;
DROP TABLE IF EXISTS job;
DROP TABLE IF EXISTS customer;

CREATE TABLE customer (
    customer_id    TEXT PRIMARY KEY,
    customer_name  TEXT NOT NULL,
    customer_type  TEXT NOT NULL,   -- production_builder | general_contractor | dealer | retail
    payment_terms  TEXT NOT NULL,   -- net_30 | net_45 | net_60 | deposit_50
    credit_limit   REAL,            -- NULL for retail: no credit line on file
    is_active      INTEGER NOT NULL
);

CREATE TABLE job (
    job_no              TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL,
    subdivision         TEXT,        -- NULL on commercial work: there is no subdivision
    lot_no              TEXT,        -- NULL on commercial work
    plan_name           TEXT,        -- builder's floor plan name, NULL on commercial
    address             TEXT NOT NULL,
    city                TEXT NOT NULL,
    state               TEXT NOT NULL,   -- 原始录入值，大小写不统一（CA 与 Ca 混用），别拿它筛选
    -- 规范化后的州。生成列而不是让每条查询自己写 UPPER()：
    -- 语义层里把「必须写 UPPER(state)」写到最硬的措辞，三轮压测里仍有两轮写成
    -- `state = 'CA'`，静默漏掉 3% 的行。描述能引导，不能保证——真 ERP 里这类
    -- 脏数据同样改不了源头，通行做法就是在报表层建一个规范化的列或视图，
    -- 让查询有一个无歧义的目标，而不是指望每次都记得加函数。
    -- 普通列而不是 GENERATED：SQLite 的 PRAGMA table_info 不返回生成列，
    -- 语义层校验（ai_sql_semantic validate）会判定「元数据里有、真实库里没有」。
    -- 物化一列也更接近真 ERP 报表层的做法。
    state_norm          TEXT NOT NULL,
    job_type            TEXT NOT NULL,   -- production | semi_custom | commercial | service
    contract_amount     REAL NOT NULL,   -- excludes tax
    sold_date           TEXT NOT NULL,
    target_install_date TEXT,            -- NULL until released to production
    job_status          TEXT NOT NULL,   -- sold | released | in_production | shipped | installed | closed | cancelled
    FOREIGN KEY (customer_id) REFERENCES customer(customer_id)
);

CREATE TABLE job_item (
    item_id        TEXT PRIMARY KEY,
    job_no         TEXT NOT NULL,
    line_no        INTEGER NOT NULL,
    category       TEXT NOT NULL,   -- base_cabinet | wall_cabinet | tall_cabinet | vanity | countertop | hardware | molding | accessory
    sku            TEXT NOT NULL,
    description    TEXT NOT NULL,
    qty            INTEGER NOT NULL,
    unit_price     REAL NOT NULL,
    extended_price REAL NOT NULL,
    FOREIGN KEY (job_no) REFERENCES job(job_no)
);

CREATE TABLE change_order (
    change_order_no TEXT PRIMARY KEY,
    job_no          TEXT NOT NULL,
    reason          TEXT NOT NULL,   -- door_style_change | added_cabinets | size_change | finish_change | builder_credit | field_conflict
    amount_delta    REAL NOT NULL,   -- negative on credits back to the builder
    requested_date  TEXT NOT NULL,
    approved_date   TEXT,            -- NULL while pending
    co_status       TEXT NOT NULL,   -- pending | approved | rejected
    FOREIGN KEY (job_no) REFERENCES job(job_no)
);

CREATE TABLE invoice (
    invoice_no     TEXT PRIMARY KEY,
    job_no         TEXT NOT NULL,
    invoice_type   TEXT NOT NULL,   -- deposit | progress | final
    invoice_date   TEXT NOT NULL,
    amount         REAL NOT NULL,
    due_date       TEXT NOT NULL,
    invoice_status TEXT NOT NULL,   -- open | partial | paid | void
    FOREIGN KEY (job_no) REFERENCES job(job_no)
);

CREATE TABLE payment_received (
    payment_id    TEXT PRIMARY KEY,
    invoice_no    TEXT NOT NULL,
    amount        REAL NOT NULL,
    received_date TEXT NOT NULL,
    method        TEXT NOT NULL,   -- check | ach | credit_card | wire
    FOREIGN KEY (invoice_no) REFERENCES invoice(invoice_no)
);

CREATE INDEX idx_job_customer ON job(customer_id);
CREATE INDEX idx_job_status   ON job(job_status);
CREATE INDEX idx_item_job     ON job_item(job_no);
CREATE INDEX idx_inv_job      ON invoice(job_no);
CREATE INDEX idx_pay_invoice  ON payment_received(invoice_no);
