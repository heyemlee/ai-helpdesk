"""
Build the simulated ERP: four SQLite databases with ~30k rows of US cabinet-shop data.

**What this is for.** The four demo databases that ship with this project hold about
100 rows between them -- 8 sales orders, 9 work orders. That is enough to prove a
query runs and nowhere near enough to find out where the system is wrong. Aggregate
questions ("how many jobs did we sell last month") are meaningless at 8 rows, the
planner never has to filter because everything fits in one glance, and whole
categories of customer question have no data behind them at all.

This dataset is built to be **stressed**, not demoed. Every awkward shape a real
business has is in here on purpose, and every one is listed in EDGE CASES below.

**What it is NOT.** A real ERP extract looks nothing like this: tables named
`T_SAL_ORDER`, columns named `FBILLNO`, statuses stored as single letters, no
comments. Column names here are business-plain. That gap is the single largest
piece of work in a real ERP connection and this dataset does **not** rehearse it --
see tools/sim_erp/README.md so nobody mistakes one for the other.

Deterministic: same SEED gives byte-identical databases, so a benchmark run is
reproducible and a diff in results means the code changed, not the data.
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

SEED = 20260903

# The business runs from 2025-01 to "today". Time-relative questions ("jobs sold
# last month", "deliveries due this week") only mean anything if the data reaches
# the present, so the window ends at TODAY rather than at some fixed past date.
START_DATE = date(2025, 1, 1)
TODAY = date(2026, 9, 3)

N_CUSTOMERS = 42
N_JOBS = 1200
N_MATERIALS = 140

# ── Business vocabulary ───────────────────────────────────────────────────────

BUILDER_NAMES = [
    "Marlowe Homes", "Marlowe Homes LLC",   # near-duplicate on purpose (edge case 13)
    "Lenbrook Central Valley", "JR Holloway Sacramento", "Kestrel Home Bay Area",
    "Tamsin Morrow West", "Shorewind Homes NorCal", "Woodwren Homes",
    "Tri Harbor Homes", "Cinder Ridge Communities", "LMR Homes West",
    "Bexley Homes California", "Ridgeway American", "Pembrook Group North",
]
GC_NAMES = [
    "Swanmere Builders", "Delcrest Construction", "XK Construction",
    "Rowan and Slate", "BRBuilders", "Tier 9 Construction",
    "Truemont Construction", "Overbrook Construction",
]
DEALER_NAMES = [
    "Bay Area Kitchen Gallery", "Capital Cabinet Supply", "Delta Design Center",
    "Sierra Kitchen and Bath", "Valley Cabinet Outlet",
]
RETAIL_NAMES = [
    "Nguyen Residence", "Patel Residence", "Okafor Residence", "Silva Residence",
    "Cohen Residence", "Ramirez Residence", "Tanaka Residence", "Fitzgerald Residence",
    "Boateng Residence", "Kowalski Residence", "Haddad Residence", "Lindqvist Residence",
    "Adeyemi Residence", "Vasquez Residence", "Thompson Residence",
]

SUBDIVISIONS = [
    "Sterling Ranch", "Copper Creek", "Harvest Grove", "Willow Bend",
    "Stone Gate", "Cedar Point", "Amber Ridge", "Foxtail Commons",
    "Blue Oak Estates", "Larkspur Landing", "Sunfield", "Vista Del Sol",
]
PLAN_NAMES = ["Plan 1420", "Plan 1685", "Plan 1970", "Plan 2240", "Plan 2515", "Plan 2890"]
CITIES = [
    ("Sacramento", "CA"), ("Roseville", "CA"), ("Elk Grove", "CA"), ("Folsom", "CA"),
    ("Stockton", "CA"), ("Modesto", "CA"), ("Tracy", "CA"), ("Fremont", "CA"),
    ("San Jose", "CA"), ("Reno", "NV"), ("Sparks", "NV"), ("Carson City", "NV"),
]
STREETS = [
    "Aspen Way", "Marigold Ct", "Ridgeline Dr", "Copper Leaf Ln", "Quail Run",
    "Sagebrush Ave", "Meadowlark St", "Juniper Hollow", "Sandstone Dr", "Wheatfield Ln",
]

ITEM_CATALOG = [
    # (category, sku prefix, description, low price, high price)
    ("base_cabinet",  "B",   "Base cabinet {w}\" 1-drawer 2-door",      210, 480),
    ("base_cabinet",  "DB",  "Drawer base {w}\" 3-drawer",              260, 520),
    ("wall_cabinet",  "W",   "Wall cabinet {w}\"x30\" 2-door",          165, 390),
    ("wall_cabinet",  "WDC", "Wall diagonal corner {w}\"",              240, 430),
    ("tall_cabinet",  "TP",  "Tall pantry {w}\"x84\"",                  520, 980),
    ("tall_cabinet",  "OV",  "Oven cabinet {w}\"x84\"",                 610, 1150),
    ("vanity",        "V",   "Vanity {w}\" 2-door",                     280, 640),
    ("countertop",    "CT",  "Quartz countertop, {w} sqft",              38,   82),
    ("countertop",    "CTG", "Granite countertop, {w} sqft",             34,   74),
    ("hardware",      "HW",  "Soft-close hinge set",                      4,   12),
    ("hardware",      "HWP", "Cabinet pull, brushed nickel",              3,    9),
    ("molding",       "CM",  "Crown molding, {w} lf",                     9,   22),
    ("molding",       "LM",  "Light rail molding, {w} lf",                7,   16),
    ("accessory",     "TO",  "Trash pullout",                            95,  185),
    ("accessory",     "LS",  "Lazy susan corner unit",                  140,  290),
]

DOOR_STYLES = ["Shaker White", "Shaker Grey", "Flat Panel Maple", "Raised Panel Cherry",
               "Slab Walnut", "Shaker Navy", "Beaded Inset White"]

SUPPLIER_NAMES = [
    ("Northwest Door Co", "door"), ("Cascade Panel Works", "door"),
    ("Redwood Component", "box"), ("Valley Plywood Supply", "box"),
    ("Bellhinge Distribution West", "hardware"), ("Halden Regional", "hardware"),
    ("Granfield America Dist", "hardware"), ("Sierra Stone Surfaces", "countertop"),
    ("Pacific Quartz Group", "countertop"), ("ClearLac Finishes", "finish"),
    ("Sherbrooke Industrial", "finish"), ("Golden State Hardwoods", "box"),
]

STAGES = ["cut", "edgeband", "drill", "assembly", "finish", "qc", "pack"]
OPERATORS = ["A. Ruiz", "M. Chen", "J. Okonkwo", "D. Patel", "S. Novak", "T. Ferreira",
             "K. Adeyemi", "L. Brennan", "R. Yamamoto", "C. Delgado"]
MACHINES = {"cut": ["Saw-01", "Saw-02", "CNC-01"], "edgeband": ["EB-01", "EB-02"],
            "drill": ["Drill-01", "CNC-02"], "finish": ["Line-A", "Line-B"], "qc": [None]}
DEFECT_CODES = ["FIN-SCRATCH", "FIN-ORANGEPEEL", "DIM-OVERSIZE", "DIM-UNDERSIZE",
                "HW-MISALIGN", "MAT-VOID", "ASM-GAP"]


def _d(d: date) -> str:
    return d.isoformat()


def _money(rng: random.Random, low: float, high: float) -> float:
    return round(rng.uniform(low, high), 2)


def _connect(path: Path, ddl: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(ddl.read_text(encoding="utf-8"))
    return conn


TERMS_DAYS = {"net_30": 30, "net_45": 45, "net_60": 60, "deposit_50": 15}


# ── ERP ───────────────────────────────────────────────────────────────────────

def build_erp(conn: sqlite3.Connection, rng: random.Random) -> list[dict]:
    """Customers, jobs, line items, change orders, invoices, payments received.

    Returns the job list so the other three databases can hang off the same job_no
    -- exactly as they would in production, where the link is a naming convention
    between separate systems and nothing enforces it.
    """
    roster = (
        [(n, "production_builder") for n in BUILDER_NAMES]
        + [(n, "general_contractor") for n in GC_NAMES]
        + [(n, "dealer") for n in DEALER_NAMES]
        + [(n, "retail") for n in RETAIL_NAMES]
    )[:N_CUSTOMERS]

    cust_rows, customers = [], []
    for i, (name, ctype) in enumerate(roster, start=1):
        cid = f"C{i:04d}"
        terms = {"production_builder": rng.choice(["net_30", "net_45"]),
                 "general_contractor": rng.choice(["net_45", "net_60"]),
                 "dealer": "net_30", "retail": "deposit_50"}[ctype]
        # EDGE CASE 4: retail carries no credit line -> NULL, not 0. AVG(credit_limit)
        # and `credit_limit > x` both silently drop these rows.
        credit = None if ctype == "retail" else round(rng.choice([50, 100, 150, 250, 400]) * 1000.0, 2)
        active = 0 if rng.random() < 0.07 else 1   # dormant accounts still in the book
        cust_rows.append((cid, name, ctype, terms, credit, active))
        customers.append({"id": cid, "type": ctype, "terms": terms})
    conn.executemany("INSERT INTO customer VALUES (?,?,?,?,?,?)", cust_rows)

    span = (TODAY - START_DATE).days
    jobs, job_rows, item_rows, co_rows, inv_rows, pay_rows = [], [], [], [], [], []
    item_seq = co_seq = inv_seq = pay_seq = 0

    for i in range(1, N_JOBS + 1):
        cust = rng.choice(customers)
        sold = START_DATE + timedelta(days=rng.randint(0, span))
        job_no = f"J{sold.year % 100}-{i:05d}"

        if cust["type"] == "general_contractor":
            job_type = "commercial"
        elif cust["type"] == "retail":
            job_type = rng.choice(["semi_custom", "service"])
        else:
            job_type = "production" if rng.random() < 0.8 else "semi_custom"

        # EDGE CASE 3: commercial work has no subdivision / lot / plan -- those columns
        # are NULL for a whole job type, so "what lot is this on" has no answer there.
        if job_type == "commercial":
            subdivision = lot_no = plan_name = None
        else:
            subdivision = rng.choice(SUBDIVISIONS)
            # EDGE CASE 16: lot numbers repeat across subdivisions. "lot 42" alone is
            # ambiguous; it only identifies a job together with the subdivision.
            lot_no = str(rng.randint(1, 90))
            plan_name = rng.choice(PLAN_NAMES)

        city, state = rng.choice(CITIES)
        # EDGE CASE 14: hand-keyed casing drift. WHERE state = 'CA' misses these.
        if rng.random() < 0.03:
            state = state.capitalize()

        roll = rng.random()
        status = ("cancelled" if roll < 0.04 else      # EDGE CASE 1
                  "sold" if roll < 0.10 else           # EDGE CASE 2: never released
                  "released" if roll < 0.22 else
                  "in_production" if roll < 0.42 else
                  "shipped" if roll < 0.55 else
                  "installed" if roll < 0.78 else "closed")

        target = (None if status in ("sold", "cancelled")
                  else _d(sold + timedelta(days=rng.randint(35, 120))))
        lo, hi = {"production": (9000, 26000), "semi_custom": (14000, 48000),
                  "commercial": (35000, 240000), "service": (400, 3500)}[job_type]
        contract = _money(rng, lo, hi)

        job_rows.append((job_no, cust["id"], subdivision, lot_no, plan_name,
                         f"{rng.randint(100, 9899)} {rng.choice(STREETS)}",
                         city, state, state.upper().strip(),
                         job_type, contract, _d(sold), target, status))
        jobs.append({"job_no": job_no, "status": status, "sold": sold, "type": job_type,
                     "target": target, "city": city, "state": state})

        for line in range(1, (3 if job_type == "service" else rng.randint(5, 12)) + 1):
            cat, prefix, desc, plo, phi = rng.choice(ITEM_CATALOG)
            width = rng.choice([12, 15, 18, 21, 24, 30, 33, 36, 42])
            qty = rng.randint(1, 6) if cat in ("hardware", "molding") else rng.randint(1, 3)
            unit = _money(rng, plo, phi)
            item_seq += 1
            item_rows.append((f"IT{item_seq:07d}", job_no, line, cat,
                              f"{prefix}{width}-{rng.randint(100, 999)}",
                              desc.format(w=width), qty, unit, round(unit * qty, 2)))

        if status != "cancelled" and rng.random() < 0.25:
            for _ in range(rng.randint(1, 2)):
                co_seq += 1
                reason = rng.choice(["door_style_change", "added_cabinets", "size_change",
                                     "finish_change", "builder_credit", "field_conflict"])
                # EDGE CASE 5: builder credits are negative. Totalling change orders with
                # anything but SUM() over the signed column gives the wrong number.
                delta = (-_money(rng, 200, 2400) if reason == "builder_credit"
                         else _money(rng, 150, 6800))
                req = sold + timedelta(days=rng.randint(5, 60))
                pending = rng.random() < 0.18   # EDGE CASE 5b: approved_date NULL while pending
                co_rows.append((f"CO{co_seq:05d}", job_no, reason, delta, _d(req),
                                None if pending else _d(req + timedelta(days=rng.randint(1, 14))),
                                "pending" if pending else
                                ("rejected" if rng.random() < 0.1 else "approved")))

        if status == "cancelled":
            continue

        schedule = ([("deposit", 0.5), ("final", 0.5)] if cust["type"] == "retail"
                    else [("deposit", 0.3), ("progress", 0.4), ("final", 0.3)])
        for inv_type, share in schedule:
            offset = {"deposit": 3, "progress": 30, "final": 62}[inv_type]
            inv_date = sold + timedelta(days=offset)
            if inv_date > TODAY:
                continue
            if inv_type == "final" and status in ("sold", "released", "in_production"):
                continue          # nothing final-billed before it ships
            inv_seq += 1
            inv_no = f"INV{inv_seq:06d}"
            amount = round(contract * share, 2)
            due = inv_date + timedelta(days=TERMS_DAYS[cust["terms"]])

            paid_roll = rng.random()
            if paid_roll < 0.62:
                inv_status, paid_share = "paid", 1.0
            elif paid_roll < 0.78:
                # EDGE CASE 7: partial payment. Balance due is amount - SUM(payments),
                # which cannot be read off the invoice row alone.
                inv_status, paid_share = "partial", rng.uniform(0.25, 0.8)
            elif paid_roll < 0.97:
                inv_status, paid_share = "open", 0.0
            else:
                inv_status, paid_share = "void", 0.0

            inv_rows.append((inv_no, job_no, inv_type, _d(inv_date), amount, _d(due), inv_status))

            if paid_share > 0:
                n_pay = 1 if inv_status == "paid" and rng.random() < 0.75 else rng.randint(1, 2)
                total_paid = round(amount * paid_share, 2)
                for k in range(n_pay):
                    pay_seq += 1
                    chunk = round(total_paid / n_pay, 2) if k < n_pay - 1 else round(
                        total_paid - round(total_paid / n_pay, 2) * (n_pay - 1), 2)
                    # EDGE CASE 6b: some payments land after the due date -- that is
                    # what makes "who paid late" answerable rather than hypothetical.
                    recv = inv_date + timedelta(days=rng.randint(5, TERMS_DAYS[cust["terms"]] + 25))
                    if recv > TODAY:
                        recv = TODAY
                    pay_rows.append((f"PM{pay_seq:06d}", inv_no, chunk, _d(recv),
                                     rng.choice(["check", "ach", "ach", "credit_card", "wire"])))

    conn.executemany("INSERT INTO job VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", job_rows)
    conn.executemany("INSERT INTO job_item VALUES (?,?,?,?,?,?,?,?,?)", item_rows)
    conn.executemany("INSERT INTO change_order VALUES (?,?,?,?,?,?,?)", co_rows)
    conn.executemany("INSERT INTO invoice VALUES (?,?,?,?,?,?,?)", inv_rows)
    conn.executemany("INSERT INTO payment_received VALUES (?,?,?,?,?)", pay_rows)
    conn.commit()
    return jobs


# ── Production ────────────────────────────────────────────────────────────────

def build_prod(conn: sqlite3.Connection, rng: random.Random, jobs: list[dict]) -> None:
    """Work orders, per-stage logs and QC checks."""
    wo_rows, log_rows, qc_rows = [], [], []
    wo_seq = log_seq = qc_seq = 0

    for job in jobs:
        # EDGE CASE 2: `sold` jobs were never released, `cancelled` ones died before
        # the floor. Neither has a work order -- so a job -> work_order join drops
        # about 14% of the book, and an INNER JOIN hides that fact completely.
        if job["status"] in ("sold", "cancelled"):
            continue

        wo_seq += 1
        wo_no = f"WO{wo_seq:06d}"
        scheduled = job["sold"] + timedelta(days=rng.randint(20, 70))

        if job["status"] in ("shipped", "installed", "closed"):
            stage, wo_status = "done", "complete"
            completed = scheduled + timedelta(days=rng.randint(3, 18))
            if completed > TODAY:
                completed = TODAY - timedelta(days=1)
        elif job["status"] == "in_production":
            # EDGE CASE 8: work in flight has completed_date NULL. MAX(completed_date)
            # and date filters on it quietly exclude everything currently running.
            stage = rng.choice(STAGES)
            wo_status, completed = "in_progress", None
        else:                                   # released
            stage, wo_status, completed = "cut", "scheduled", None

        # EDGE CASE 8b: a slice sits on hold with a reason; everything else has
        # hold_reason NULL. "Why is my job stuck" is only answerable for these.
        hold_reason = None
        if wo_status == "in_progress" and rng.random() < 0.08:
            wo_status = "on_hold"
            hold_reason = rng.choice(["waiting on door delivery", "field measure conflict",
                                      "customer hold - finish selection",
                                      "material shortage", "damaged parts - remake"])

        priority = "rush" if rng.random() < 0.12 else "normal"
        wo_rows.append((wo_no, job["job_no"], rng.choice(["Plant 1", "Plant 2"]),
                        stage, wo_status, priority, _d(scheduled),
                        _d(completed) if completed else None, hold_reason))

        reached = STAGES if stage == "done" else STAGES[: STAGES.index(stage) + 1]
        cursor = scheduled
        for st in reached:
            log_seq += 1
            started = cursor + timedelta(hours=rng.randint(1, 20))
            done_stage = not (st == stage and wo_status in ("in_progress", "on_hold"))
            finished = started + timedelta(hours=rng.randint(2, 30)) if done_stage else None
            log_rows.append((f"LG{log_seq:07d}", wo_no, st,
                             started.isoformat(), finished.isoformat() if finished else None,
                             rng.choice(OPERATORS),
                             rng.choice(MACHINES.get(st, [None]))))
            cursor = finished or started

        if "qc" in reached:
            qc_seq += 1
            total = rng.randint(8, 60)
            failed = 0 if rng.random() < 0.72 else rng.randint(1, max(1, total // 6))
            qc_rows.append((f"QC{qc_seq:06d}", wo_no,
                            _d((completed or scheduled) - timedelta(days=rng.randint(0, 3))),
                            rng.choice(OPERATORS), total - failed, failed,
                            # EDGE CASE 10: defect_code is NULL when nothing failed.
                            # COUNT(defect_code) != COUNT(*) and that is the point.
                            rng.choice(DEFECT_CODES) if failed else None))

    conn.executemany("INSERT INTO work_order VALUES (?,?,?,?,?,?,?,?,?)", wo_rows)
    conn.executemany("INSERT INTO stage_log VALUES (?,?,?,?,?,?,?)", log_rows)
    conn.executemany("INSERT INTO qc_check VALUES (?,?,?,?,?,?,?)", qc_rows)
    conn.commit()


# ── Warehouse ─────────────────────────────────────────────────────────────────

def build_wms(conn: sqlite3.Connection, rng: random.Random) -> list[str]:
    sup_rows, sup_ids = [], []
    for i, (name, cat) in enumerate(SUPPLIER_NAMES, start=1):
        sid = f"S{i:03d}"
        sup_ids.append(sid)
        sup_rows.append((sid, name, cat, round(rng.uniform(0.62, 0.99), 3), rng.randint(5, 45)))
    conn.executemany("INSERT INTO supplier VALUES (?,?,?,?,?)", sup_rows)

    mat_rows, inv_rows, skus = [], [], []
    for i in range(1, N_MATERIALS + 1):
        cat, prefix, desc, lo, hi = rng.choice(ITEM_CATALOG)
        sku = f"{prefix}-{1000 + i}"
        skus.append(sku)
        uom = {"countertop": "sqft", "molding": "lf"}.get(cat, "each")
        safety = rng.choice([0, 10, 25, 50, 100])
        mat_rows.append((sku, desc.format(w=rng.choice([12, 24, 36])), cat, uom,
                         safety, _money(rng, lo * 0.35, hi * 0.5)))
        on_hand = rng.randint(0, 400)
        # EDGE CASE 15: SKUs that have never been cycle-counted have last_counted NULL.
        last_counted = (None if rng.random() < 0.15
                        else _d(TODAY - timedelta(days=rng.randint(1, 400))))
        inv_rows.append((sku, on_hand, min(on_hand, rng.randint(0, 120)),
                         f"{rng.choice('ABCD')}-{rng.randint(1, 40):02d}", last_counted))
    conn.executemany("INSERT INTO material VALUES (?,?,?,?,?,?)", mat_rows)
    conn.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", inv_rows)

    rec_rows = []
    for i in range(1, 1801):
        sku = rng.choice(skus)
        promised = TODAY - timedelta(days=rng.randint(0, 560))
        # EDGE CASE 17: outstanding receipts have received_date NULL. A late-delivery
        # query that only looks at received > promised misses everything never received.
        if rng.random() < 0.09:
            received = None
        else:
            received = promised + timedelta(days=rng.choice([0, 0, 0, 1, 2, 3, 7, -1]))
            received = None if received > TODAY else received
        rec_rows.append((f"RC{i:06d}", sku, rng.choice(sup_ids), rng.randint(5, 500),
                         _d(promised), _d(received) if received else None,
                         f"LOT{rng.randint(10000, 99999)}"))
    conn.executemany("INSERT INTO receipt VALUES (?,?,?,?,?,?,?)", rec_rows)
    conn.commit()
    return skus


# ── Delivery and service ──────────────────────────────────────────────────────

def build_svc(conn: sqlite3.Connection, rng: random.Random, jobs: list[dict],
              skus: list[str]) -> None:
    """Deliveries, service tickets and replacement parts.

    These three tables exist because of what the helpdesk is actually asked. A probe
    of 12 real phrasings found "when is my delivery" and "I'm missing parts" had no
    data behind them anywhere -- not a model problem, a modelling gap.
    """
    del_rows, tkt_rows, part_rows = [], [], []
    del_seq = tkt_seq = part_seq = 0

    for job in jobs:
        if job["status"] in ("sold", "released", "cancelled"):
            continue

        del_seq += 1
        scheduled = job["sold"] + timedelta(days=rng.randint(40, 110))
        if job["status"] in ("shipped", "installed", "closed"):
            delivered = scheduled + timedelta(days=rng.choice([0, 0, 0, 1, 2, 5]))
            delivered = None if delivered > TODAY else delivered
            status = "delivered" if delivered else "in_transit"
        else:
            delivered, status = None, "scheduled"

        notes = None
        roll = rng.random()
        if roll < 0.05:
            # EDGE CASE 11: a failed delivery keeps its original row and gets a note.
            status, delivered, notes = "failed", None, rng.choice(
                ["site not ready", "no one on site to receive", "access blocked by other trades"])
        elif roll < 0.10:
            status, delivered, notes = "rescheduled", None, "builder requested new date"
        # EDGE CASE 11b: deliveries whose scheduled date has passed with no delivered_date
        # are LATE. That is the whole point of leaving delivered_date nullable.

        del_rows.append((f"DL{del_seq:06d}", job["job_no"], _d(scheduled),
                         _d(delivered) if delivered else None,
                         rng.choice(["own_truck", "own_truck", "ltl_freight", "customer_pickup"]),
                         status, f"{job['city']}, {job['state']}", notes))

        if rng.random() < 0.33:
            for _ in range(rng.randint(1, 2)):
                tkt_seq += 1
                tkt_no = f"TK{tkt_seq:06d}"
                issue = rng.choice(["missing_part", "missing_part", "damage", "defect",
                                    "install_issue", "wrong_item"])
                reported = scheduled + timedelta(days=rng.randint(0, 30))
                if reported > TODAY:
                    reported = TODAY - timedelta(days=rng.randint(0, 5))
                open_still = rng.random() < 0.28
                tstatus = (rng.choice(["open", "in_progress", "waiting_parts"]) if open_still
                           else rng.choice(["resolved", "closed"]))
                # EDGE CASE 12: open tickets have resolved_date NULL. "Average days to
                # resolve" computed without excluding them is silently wrong.
                resolved = None if open_still else _d(reported + timedelta(days=rng.randint(1, 25)))
                tkt_rows.append((tkt_no, job["job_no"], issue, _d(reported), resolved, tstatus,
                                 {"missing_part": "Missing parts reported at install",
                                  "damage": "Freight damage found on unload",
                                  "defect": "Finish defect on door front",
                                  "install_issue": "Filler does not fit field condition",
                                  "wrong_item": "Wrong door style shipped"}[issue],
                                 rng.choice(["builder superintendent", "installer", "homeowner"])))

                if issue in ("missing_part", "damage", "wrong_item"):
                    for _ in range(rng.randint(1, 3)):
                        part_seq += 1
                        req_date = reported + timedelta(days=rng.randint(0, 4))
                        pstatus = (rng.choice(["requested", "in_production", "shipped"])
                                   if open_still else rng.choice(["shipped", "received"]))
                        shipped = (None if pstatus in ("requested", "in_production")
                                   else _d(req_date + timedelta(days=rng.randint(3, 20))))
                        part_rows.append((f"RP{part_seq:06d}", tkt_no, rng.choice(skus),
                                          rng.randint(1, 4), _d(req_date), shipped, pstatus))

    conn.executemany("INSERT INTO delivery VALUES (?,?,?,?,?,?,?,?)", del_rows)
    conn.executemany("INSERT INTO service_ticket VALUES (?,?,?,?,?,?,?,?)", tkt_rows)
    conn.executemany("INSERT INTO replacement_part VALUES (?,?,?,?,?,?,?)", part_rows)
    conn.commit()


# ── Entry point ───────────────────────────────────────────────────────────────

def build(out_dir: Path) -> dict[str, int]:
    rng = random.Random(SEED)
    sql_dir = Path(__file__).resolve().parent / "sql"
    out_dir.mkdir(parents=True, exist_ok=True)

    erp = _connect(out_dir / "erp.db", sql_dir / "erp.sql")
    jobs = build_erp(erp, rng)

    prod = _connect(out_dir / "prod.db", sql_dir / "prod.sql")
    build_prod(prod, rng, jobs)

    wms = _connect(out_dir / "wms.db", sql_dir / "wms.sql")
    skus = build_wms(wms, rng)

    svc = _connect(out_dir / "svc.db", sql_dir / "svc.sql")
    build_svc(svc, rng, jobs, skus)

    counts: dict[str, int] = {}
    for name, conn in (("erp", erp), ("prod", prod), ("wms", wms), ("svc", svc)):
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall():
            counts[f"{name}.{table}"] = conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the simulated ERP databases.")
    ap.add_argument("--out", default="runtime_data/sim", help="output directory")
    args = ap.parse_args()
    out = Path(args.out)
    counts = build(out)
    width = max(len(k) for k in counts)
    for k, v in counts.items():
        print(f"  {k:<{width}}  {v:>7,}")
    print(f"\n  {'total':<{width}}  {sum(counts.values()):>7,}  ->  {out}")


if __name__ == "__main__":
    main()
