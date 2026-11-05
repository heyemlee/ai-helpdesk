-- Simulated production system (MES). Separate system from ERP: no cross-database FKs,
-- and job_no is the only thing tying a work order back to an order -- by naming
-- convention, not by constraint. Getting that link wrong is silent: the SQL runs,
-- returns rows, and the rows are wrong.
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS qc_check;
DROP TABLE IF EXISTS stage_log;
DROP TABLE IF EXISTS work_order;

CREATE TABLE work_order (
    wo_no          TEXT PRIMARY KEY,
    job_no         TEXT NOT NULL,      -- ties to erp_db.job.job_no by naming convention only
    plant          TEXT NOT NULL,      -- Plant 1 | Plant 2
    current_stage  TEXT NOT NULL,      -- cut | edgeband | drill | assembly | finish | qc | pack | done
    wo_status      TEXT NOT NULL,      -- scheduled | in_progress | on_hold | complete | cancelled
    priority       TEXT NOT NULL,      -- normal | rush
    scheduled_date TEXT NOT NULL,
    completed_date TEXT,               -- NULL while the order has not finished
    hold_reason    TEXT                -- NULL unless wo_status = on_hold
);

CREATE TABLE stage_log (
    log_id      TEXT PRIMARY KEY,
    wo_no       TEXT NOT NULL,
    stage       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,                  -- NULL on the stage currently running
    operator    TEXT NOT NULL,
    machine     TEXT,                  -- NULL on hand stations (assembly, pack)
    FOREIGN KEY (wo_no) REFERENCES work_order(wo_no)
);

CREATE TABLE qc_check (
    check_id     TEXT PRIMARY KEY,
    wo_no        TEXT NOT NULL,
    checked_date TEXT NOT NULL,
    inspector    TEXT NOT NULL,
    passed_qty   INTEGER NOT NULL,
    failed_qty   INTEGER NOT NULL,
    defect_code  TEXT,                 -- NULL when nothing failed
    FOREIGN KEY (wo_no) REFERENCES work_order(wo_no)
);

CREATE INDEX idx_wo_job    ON work_order(job_no);
CREATE INDEX idx_wo_status ON work_order(wo_status);
CREATE INDEX idx_log_wo    ON stage_log(wo_no);
CREATE INDEX idx_qc_wo     ON qc_check(wo_no);
