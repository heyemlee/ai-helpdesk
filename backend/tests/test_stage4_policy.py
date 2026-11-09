"""Policy filtering, clocks, migration and concurrency on both maintained stores."""
import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.core import policy_scope as ps
from app.core.embedder import Embedder
from app.storage.vector_store import LocalStore, VectorsChangedError
from app.tools.knowledge_base import KnowledgeBase


@pytest.fixture(params=["sqlite"] + (["postgres"] if os.getenv("TEST_POSTGRES_DSN") else []))
async def kb(request, tmp_path):
    if request.param == "postgres":
        from app.storage.pg import Database
        from app.storage.vector_store import PgStore
        db = Database(os.environ["TEST_POSTGRES_DSN"])
        await db.connect(embedding_dim=Embedder().dim)
        await db.execute("TRUNCATE policy_chunks,policy_documents")
        store = PgStore(db)
    else:
        db = None
        store = LocalStore(str(tmp_path / "policy.db"))
    kb = KnowledgeBase(store=store, embedder=Embedder())
    yield kb
    if db:
        await db.close()
    else:
        await store.close()


def policy(**kwargs):
    return ps.PolicyMetadata(status="published", version="1", effective_at="2020-01-01T00:00:00Z", **kwargs).model_dump(mode="json")


async def add(kb, **kwargs):
    return (await kb.import_documents([dict(title="policy-control", content="sample shipping policy", policy=policy(**kwargs))]))[0]["id"]


async def test_unknown_scope_only_explicit_universal_before_both_topks(kb):
    universal = await add(kb)
    for i in range(16):
        await add(kb, product=f"secret-{i}")
    await kb.import_documents([dict(title="draft", content="sample shipping policy")])
    vector = await kb._embedder.embed("sample shipping policy")
    assert {r["doc_id"] for r in await kb._store.kb_search(vector, 1)} == {universal}
    from app.core.lexical import query_tokens
    assert {r["doc_id"] for r in await kb._store.kb_search_lexical(query_tokens("sample shipping policy"), vector, 1)} == {universal}
    with ps.bind_policy_scope(ps.PolicyScope(product="secret-1")):
        assert len(await kb.search_async("sample shipping policy")) == 2
    assert await kb._store.kb_count() >= 18


async def test_metadata_compare_and_set_and_clock_only_cache_revocation(kb, monkeypatch):
    instant = datetime(2026, 11, 6, tzinfo=UTC)
    monkeypatch.setattr(ps, "now", lambda: instant)
    doc_id = await add(kb, expires_at=(instant + timedelta(seconds=1)).isoformat())
    version = await kb.cache_version()
    refs = await kb.search_async("sample shipping policy")
    assert refs and await kb.validate_policy_refs(refs)
    instant += timedelta(seconds=2)
    assert version != await kb.cache_version()
    assert not await kb.search_async("sample shipping policy")
    assert not await kb.validate_policy_refs(refs)
    doc = await kb.get_document(doc_id)
    old = doc["policy_revision"]
    changes = await asyncio.gather(*(kb.set_document_policy(doc_id, ps.PolicyMetadata(version=str(i)), old) for i in (2, 3)), return_exceptions=True)
    assert sum(isinstance(r, ValueError) for r in changes) == 1


async def test_history_is_explicit_and_tools_cannot_override_scope(kb):
    await add(kb, product="restricted", expires_at="2021-01-01T00:00:00Z")
    assert not await kb.search_handler({"query":"sample shipping policy", "product":"restricted", "as_of":"2020-02-01T00:00:00Z"}, None)
    with ps.bind_policy_scope(ps.PolicyScope(product="restricted", as_of="2020-02-01T00:00:00Z")):
        assert await kb.search_handler({"query":"sample shipping policy", "product":"other"}, None)


async def test_reimport_after_metadata_change_preserves_both_policy_identities(kb):
    original = await add(kb)
    doc = await kb.get_document(original)
    await kb.set_document_policy(original, ps.PolicyMetadata.model_validate(policy(product="restricted")), doc["policy_revision"])
    replacement = await add(kb)
    assert replacement != original
    assert await add(kb) == replacement
    assert await add(kb, product="restricted") == original
    assert {h["doc_id"] for h in await kb.search_async("sample shipping policy")} == {replacement}


async def test_legacy_names_are_empty_and_reject_writes(kb):
    await add(kb)
    store = kb._store
    if store.backend == "sqlite_local":
        assert store._db.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            store._db.execute("DELETE FROM kb_documents")
    else:
        assert (await store._db.fetchrow("SELECT COUNT(*) AS n FROM kb_documents"))["n"] == 0
        with pytest.raises(Exception, match="view"):
            await store._db.execute("DELETE FROM kb_documents")
        rows = await store.kb_embedding_rows()
        episodic = await store.episodic_embedding_rows()
        vec = [0.0] * Embedder().dim
        await store.replace_embeddings(len(vec), {r['id']:vec for r in rows}, {r['id']:vec for r in episodic}, "test")
        with pytest.raises(VectorsChangedError):
            await store.replace_embeddings(len(vec), {}, {}, "failed")
        assert (await store._db.fetchrow("SELECT COUNT(*) AS n FROM kb_chunks"))["n"] == 0


def test_local_migration_is_atomic_and_preserves_legacy_on_failure(tmp_path, monkeypatch):
    from app.storage import policy_migration
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kb_documents(id TEXT PRIMARY KEY,title TEXT,content TEXT,content_hash TEXT,origin TEXT,source_type TEXT,source_name TEXT,chunk_count INT,created_at REAL)")
    conn.execute("INSERT INTO kb_documents VALUES('old','title','retained','hash','original','','',0,0)")
    conn.commit()
    original = policy_migration.local_seal
    def fail(db):
        # A second connection cannot take the schema write lock mid-migration.
        other = sqlite3.connect(path, timeout=0.01)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("CREATE TABLE kb_chunks(id TEXT)")
        other.close()
        raise RuntimeError("interrupted")
    monkeypatch.setattr(policy_migration, "local_seal", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        LocalStore(path)
    assert conn.execute("SELECT content FROM kb_documents").fetchone()[0] == "retained"
    monkeypatch.setattr(policy_migration, "local_seal", original)
    store = LocalStore(path)
    assert store._db.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0] == 0
    assert json.loads(store._db.execute("SELECT policy FROM policy_documents").fetchone()[0]) == {}
    store._db.close()
    conn.close()
