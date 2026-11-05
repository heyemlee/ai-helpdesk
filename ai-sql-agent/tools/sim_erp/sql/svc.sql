-- Simulated delivery and after-sales service.
--
-- This database exists because of what the helpdesk actually gets asked. A probe
-- of 12 real phrasings found two whole question categories that no amount of
-- model tuning could answer, because the data was not modelled anywhere:
-- "when is my delivery" and "I am missing parts". Those are these tables.
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS replacement_part;
DROP TABLE IF EXISTS service_ticket;
DROP TABLE IF EXISTS delivery;

CREATE TABLE delivery (
    delivery_no     TEXT PRIMARY KEY,
    job_no          TEXT NOT NULL,   -- naming-convention link to erp_db.job
    scheduled_date  TEXT NOT NULL,
    delivered_date  TEXT,            -- NULL when not yet delivered (late if scheduled_date has passed)
    carrier         TEXT NOT NULL,   -- own_truck | ltl_freight | customer_pickup
    delivery_status TEXT NOT NULL,   -- scheduled | in_transit | delivered | failed | rescheduled
    address         TEXT NOT NULL,
    notes           TEXT             -- NULL on clean deliveries
);

CREATE TABLE service_ticket (
    ticket_no     TEXT PRIMARY KEY,
    job_no        TEXT NOT NULL,
    issue_type    TEXT NOT NULL,   -- missing_part | damage | defect | install_issue | wrong_item
    reported_date TEXT NOT NULL,
    resolved_date TEXT,            -- NULL while open
    ticket_status TEXT NOT NULL,   -- open | in_progress | waiting_parts | resolved | closed
    description   TEXT NOT NULL,
    reported_by   TEXT NOT NULL    -- builder superintendent, installer, homeowner
);

CREATE TABLE replacement_part (
    request_id     TEXT PRIMARY KEY,
    ticket_no      TEXT NOT NULL,
    sku            TEXT NOT NULL,
    qty            INTEGER NOT NULL,
    requested_date TEXT NOT NULL,
    shipped_date   TEXT,            -- NULL until it ships
    request_status TEXT NOT NULL,   -- requested | in_production | shipped | received | cancelled
    FOREIGN KEY (ticket_no) REFERENCES service_ticket(ticket_no)
);

CREATE INDEX idx_delivery_job ON delivery(job_no);
CREATE INDEX idx_ticket_job   ON service_ticket(job_no);
CREATE INDEX idx_part_ticket  ON replacement_part(ticket_no);
