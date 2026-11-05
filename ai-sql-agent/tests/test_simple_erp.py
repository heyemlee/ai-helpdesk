"""Migration integrity, approved status handling and catalog integration."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3

import pytest

from ai_sql_agent.catalog import DataCatalog, PreviewQuery
from ai_sql_agent.execution import MCPRouter, SQLiteMCPExecutor
from ai_sql_agent.pipeline.objects import DatabaseConfig
from ai_sql_agent.retrieval.access import ALLOW_ALL
from ai_sql_agent.semantic import load_semantic_dir, validate_semantic_layer
from tools.sim_erp.build import build as build_source
from tools.simple_erp.build import build
from tools.simple_erp.schema import DATABASES
from tools.simple_erp.semantic import build_semantic


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    root = tmp_path_factory.mktemp("simple_erp")
    source, out, semantic = root / "source", root / "simple", root / "semantic"
    build_source(source)
    originals = {p.name: digest(p) for p in source.glob("*.db")}
    counts = build(source, out)
    build_semantic(out, semantic)
    assert originals == {p.name: digest(p) for p in source.glob("*.db")}
    return source, out, semantic, counts


def query(db, sql):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql).fetchall()


def test_migration_preserves_orders_amounts_inventory_and_references(migrated):
    source, out, _, counts = migrated
    assert counts["erp_db.orders"] == 1200
    assert counts["erp_db.order_item"] == 9064
    assert query(out / "erp.db", "SELECT SUM(contract_amount) FROM orders") == query(
        source / "erp.db", "SELECT SUM(contract_amount) FROM job")
    assert query(out / "wms.db", "SELECT SUM(on_hand_qty), SUM(allocated_qty) FROM material") == query(
        source / "wms.db", "SELECT SUM(on_hand_qty), SUM(allocated_qty) FROM inventory")
    assert query(out / "wms.db", "SELECT SUM(purchased_qty) FROM receipt") == query(
        source / "wms.db", "SELECT SUM(qty) FROM receipt")
    assert query(out / "wms.db", "SELECT SUM(received_qty) FROM receipt") == query(
        source / "wms.db", "SELECT SUM(qty) FROM receipt WHERE received_date IS NOT NULL")
    orders = {row[0] for row in query(out / "erp.db", "SELECT order_no FROM orders")}
    for filename, table in (("prod.db", "work_order"), ("svc.db", "delivery"),
                            ("svc.db", "service_ticket"), ("svc.db", "replacement_part")):
        assert {row[0] for row in query(out / filename, f"SELECT order_no FROM {table}")} <= orders
    assert query(out / "erp.db", "SELECT COUNT(*) FROM orders WHERE phone IS NOT NULL OR salesperson IS NOT NULL") == [(0,)]
    assert query(out / "svc.db", "SELECT COUNT(*) FROM replacement_part WHERE part_status='已解决'") == query(
        source / "svc.db", "SELECT COUNT(*) FROM replacement_part WHERE request_status='received'")


def test_same_name_materials_merge_without_losing_stock_or_locations(migrated):
    source, out, _, _ = migrated
    name = 'Soft-close hinge set'
    with closing(sqlite3.connect(source / "wms.db")) as conn:
        total = conn.execute("SELECT SUM(i.on_hand_qty),SUM(i.allocated_qty),SUM(m.safety_stock) "
                             "FROM material m JOIN inventory i ON i.sku=m.sku WHERE m.description=?", (name,)).fetchone()
        locations = {r[0] for r in conn.execute("SELECT i.location FROM material m JOIN inventory i ON i.sku=m.sku "
                                               "WHERE m.description=?", (name,))}
    with closing(sqlite3.connect(out / "wms.db")) as conn:
        row = conn.execute("SELECT on_hand_qty,allocated_qty,safety_stock,location FROM material WHERE material_name=?", (name,)).fetchone()
    assert row[:3] == total
    assert all(location in row[3].split("、") for location in locations)


def test_subtotals_are_automatic_and_status_values_are_enforced(migrated, tmp_path):
    _, out, _, _ = migrated
    erp = tmp_path / "erp.db"
    shutil.copyfile(out / "erp.db", erp)
    with closing(sqlite3.connect(erp)) as conn:
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute("UPDATE order_item SET quantity=3,unit_price=12.34,subtotal=999 WHERE rowid=1")
        assert conn.execute("SELECT subtotal FROM order_item WHERE rowid=1").fetchone() == (37.02,)
        order_no = conn.execute("SELECT order_no FROM orders LIMIT 1").fetchone()[0]
        conn.execute("INSERT INTO order_item(order_no,product_name,model,quantity,unit_price) VALUES (?,?,?,2,1.25)",
                     (order_no, "测试产品", "MODEL-TEST"))
        assert conn.execute("SELECT subtotal FROM order_item WHERE model='MODEL-TEST'").fetchone() == (2.5,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE orders SET order_status='installed'")
    for filename, table, column, removed in (("prod.db", "work_order", "production_status", "已暂停"),
                                              ("prod.db", "work_order", "current_stage", "质检"),
                                              ("svc.db", "service_ticket", "service_status", "待补件"),
                                              ("svc.db", "replacement_part", "part_status", "已发出")):
        target = tmp_path / f"{table}-{column}.db"
        shutil.copyfile(out / filename, target)
        with closing(sqlite3.connect(target)) as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {table} SET {column}=?", (removed,))


def test_schema_semantics_and_all_nine_catalog_previews(migrated):
    _, out, semantic, _ = migrated
    layer = load_semantic_dir(semantic)
    report = validate_semantic_layer(layer, live=True)
    assert report.ok and len(report.live_checked) == 4, report.render()
    assert not report.warnings
    assert sum(len(db.tables) for db in layer.databases.values()) == 9
    assert sum(len(t["columns"]) for db in layer.databases.values() for t in db.tables.values()) == 60
    assert "promised_date" not in layer.databases["wms_db"].tables["receipt"]["columns"]
    router, databases = MCPRouter(), []
    for db, (filename, *_rest) in DATABASES.items():
        databases.append(DatabaseConfig(name=db, db_path=out / filename))
        router.register_executor(db, SQLiteMCPExecutor(db, out / filename))
    catalog = DataCatalog(semantic=layer, databases=databases, router=router)
    description = catalog.describe(ALLOW_ALL)
    assert len(description["domains"]) == 4
    for db, database in layer.databases.items():
        for table in database.tables:
            result = catalog.preview(ALLOW_ALL, database=db, table=table, query=PreviewQuery(page_size=2))
            assert len(result["rows"]) == 2


def test_failed_conversion_preserves_existing_output(migrated, tmp_path):
    source, out, _, _ = migrated
    broken = tmp_path / "broken-source"
    destination = tmp_path / "existing"
    shutil.copytree(source, broken)
    shutil.copytree(out, destination)
    before = {p.name: digest(p) for p in destination.glob("*.db")}
    with closing(sqlite3.connect(broken / "wms.db")) as conn:
        conn.execute("UPDATE material SET uom='sheet' WHERE sku=(SELECT sku FROM material "
                     "WHERE description='Soft-close hinge set' LIMIT 1)")
        conn.commit()
    with pytest.raises(ValueError, match="incompatible units"):
        build(broken, destination)
    assert before == {p.name: digest(p) for p in destination.glob("*.db")}


def test_rebuild_backs_up_existing_database(migrated, tmp_path):
    source, out, _, _ = migrated
    destination = tmp_path / "simple"
    shutil.copytree(out, destination)
    with closing(sqlite3.connect(destination / "erp.db")) as conn:
        conn.execute("UPDATE orders SET salesperson='Test salesperson' WHERE rowid=1")
        conn.commit()
    build(source, destination)
    backups = list((tmp_path / "backups").glob("simple-erp-*/erp.db"))
    assert len(backups) == 1
    assert query(backups[0], "SELECT salesperson FROM orders WHERE rowid=1") == [("Test salesperson",)]
    assert query(destination / "erp.db", "SELECT salesperson FROM orders WHERE rowid=1") == [(None,)]
