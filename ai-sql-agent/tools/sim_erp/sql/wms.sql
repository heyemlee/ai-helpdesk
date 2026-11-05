-- Simulated materials and inventory (WMS). Separate system again.
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS inventory;
DROP TABLE IF EXISTS receipt;
DROP TABLE IF EXISTS material;
DROP TABLE IF EXISTS supplier;

CREATE TABLE supplier (
    supplier_id    TEXT PRIMARY KEY,
    supplier_name  TEXT NOT NULL,
    category       TEXT NOT NULL,   -- door | box | hardware | countertop | finish
    on_time_rate   REAL NOT NULL,   -- 0..1, share of receipts that landed on the promised date
    lead_time_days INTEGER NOT NULL
);

CREATE TABLE material (
    sku          TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    category     TEXT NOT NULL,
    uom          TEXT NOT NULL,    -- each | sheet | lf | sqft
    safety_stock INTEGER NOT NULL,
    unit_cost    REAL NOT NULL
);

CREATE TABLE receipt (
    receipt_id    TEXT PRIMARY KEY,
    sku           TEXT NOT NULL,
    supplier_id   TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    promised_date TEXT NOT NULL,
    received_date TEXT,             -- NULL while still outstanding
    lot_code      TEXT NOT NULL,
    FOREIGN KEY (sku) REFERENCES material(sku),
    FOREIGN KEY (supplier_id) REFERENCES supplier(supplier_id)
);

CREATE TABLE inventory (
    sku            TEXT PRIMARY KEY,
    on_hand_qty    INTEGER NOT NULL,
    allocated_qty  INTEGER NOT NULL,
    location       TEXT NOT NULL,
    last_counted   TEXT,            -- NULL on SKUs never cycle-counted
    FOREIGN KEY (sku) REFERENCES material(sku)
);

CREATE INDEX idx_receipt_sku      ON receipt(sku);
CREATE INDEX idx_receipt_supplier ON receipt(supplier_id);
