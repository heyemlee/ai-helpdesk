"""向量存储双后端 + KnowledgeBase 业务逻辑的行为测试。

LocalStore 直接测（零依赖）；PgStore 在设置 TEST_POSTGRES_DSN 时测
（CI 里起 pgvector 容器），本地未配置则跳过——两个后端跑同一组断言，
保证"接口一致"不是一句注释而是被验证的事实。
"""
import os
import uuid

import pytest

from core.embedder import Embedder, cosine
from mcp.knowledge_base import KnowledgeBase
from storage.vector_store import LocalStore

TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()


# ── Embedder ──────────────────────────────────────────────────────────────────

async def test_local_embedder_is_deterministic():
    emb = Embedder()  # 未配置远端 → 本地 n-gram
    assert emb.backend == "local_ngram"
    assert emb.dim == 256
    v1 = await emb.embed("我要退款")
    v2 = await emb.embed("我要退款")
    assert v1 == v2
    assert len(v1) == 256


async def test_local_embedder_similar_texts_score_higher():
    emb = Embedder()
    refund1 = await emb.embed("我要申请退款")
    refund2 = await emb.embed("申请退款怎么操作")
    logistics = await emb.embed("快递物流什么时候到")
    assert cosine(refund1, refund2) > cosine(refund1, logistics)


def test_embedder_dim_follows_remote_config():
    remote = Embedder(base_url="https://api.example.com/v1", api_key="k")
    assert remote.backend == "remote"
    assert remote.dim == 1536
    explicit = Embedder(base_url="https://api.example.com/v1", api_key="k", dim=768)
    assert explicit.dim == 768


# ── 双后端共同断言 ────────────────────────────────────────────────────────────

async def _store_contract(store, embedder):
    """两个后端必须通过的同一组断言。"""
    # KB：写入、去重过滤、检索排序
    chunks = [
        {"id": f"a-{uuid.uuid4().hex[:8]}", "title": "退款", "content": "退款政策说明",
         "chunk_index": 0, "total_chunks": 1, "ingested_at": 1.0,
         "embedding": await embedder.embed("退款政策说明")},
        {"id": f"b-{uuid.uuid4().hex[:8]}", "title": "物流", "content": "快递配送时效",
         "chunk_index": 0, "total_chunks": 1, "ingested_at": 1.0,
         "embedding": await embedder.embed("快递配送时效")},
    ]
    await store.kb_add(chunks)
    existing = await store.kb_existing_ids([chunks[0]["id"], "missing-id"])
    assert chunks[0]["id"] in existing
    assert "missing-id" not in existing

    hits = await store.kb_search(await embedder.embed("怎么申请退款"), top_k=2)
    assert hits[0]["title"] == "退款"  # 语义最近的排第一
    assert hits[0]["score"] >= hits[1]["score"]

    # 情景记忆：按用户隔离
    user_a = f"alice-{uuid.uuid4().hex[:6]}"
    user_b = f"bob-{uuid.uuid4().hex[:6]}"
    vec = await embedder.embed("上次退款的事")
    await store.episodic_add(f"e1-{user_a}", user_a, "c1", "Alice 咨询过退款", "", vec)
    await store.episodic_add(f"e2-{user_b}", user_b, "c2", "Bob 咨询过物流", "", vec)
    docs = await store.episodic_search(user_a, vec, top_k=5)
    assert docs == ["Alice 咨询过退款"]  # 检索不跨用户

    # 画像：整条覆盖
    await store.profile_set(user_a, "c1", '{"preferences": ["快递上门"]}')
    await store.profile_set(user_a, "c2", '{"preferences": ["电子发票"]}')
    raw = await store.profile_get(user_a)
    assert "电子发票" in raw and "快递上门" not in raw
    assert await store.profile_get("nobody") is None


async def test_local_store_contract(tmp_path):
    store = LocalStore(db_path=str(tmp_path / "vec.db"))
    await _store_contract(store, Embedder())
    await store.close()


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_pg_store_contract():
    from storage.pg import Database
    from storage.vector_store import PgStore

    embedder = Embedder()
    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=embedder.dim)
    try:
        await _store_contract(PgStore(db), embedder)
    finally:
        await db.close()


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_pg_ticket_and_convlog_contract():
    """PG 版工单 + 审计日志跑与 SQLite 版相同的核心断言。"""
    from storage.conversation_log import PgConversationLog
    from storage.pg import Database
    from tickets.ticket_store import PgTicketStore

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=Embedder().dim)
    try:
        tickets = PgTicketStore(db)
        t = await tickets.create(conv_id="c1", user_id="u", reason="低置信度")
        assert (await tickets.get(t.id)).status == "open"
        t = await tickets.update_status(t.id, "resolved")
        assert t.status == "resolved"
        with pytest.raises(ValueError):
            await tickets.update_status(t.id, "open")  # 终态不可回流
        assert (await tickets.stats())["total"] >= 1

        log = PgConversationLog(db)
        conv = f"conv-{uuid.uuid4().hex[:8]}"
        await log.append(conv, "u", "user", "你好")
        await log.append(conv, "u", "assistant", "您好", agent_type="general")
        entries = await log.get_conversation(conv)
        assert [e.role for e in entries] == ["user", "assistant"]
    finally:
        await db.close()


# ── KnowledgeBase 业务逻辑（在 LocalStore 上验证）─────────────────────────────

@pytest.fixture
async def kb(tmp_path):
    store = LocalStore(db_path=str(tmp_path / "kb.db"))
    yield KnowledgeBase(store=store, embedder=Embedder())
    await store.close()


async def test_kb_ingest_dedup_and_search(kb):
    n1 = await kb.add_documents_async([{"title": "退款政策", "content": "7 天无理由退款"}])
    assert n1 == 1
    n2 = await kb.add_documents_async([{"title": "退款政策", "content": "7 天无理由退款"}])
    assert n2 == 0  # 重复导入幂等
    assert await kb.doc_count_async() == 1

    results = await kb.search_async("退款", top_k=3)
    assert results and results[0]["title"] == "退款政策"
    assert 0 <= results[0]["score"] <= 1


async def test_kb_seeds_default_docs_when_empty(kb):
    await kb.ensure_seeded()
    count = await kb.doc_count_async()
    assert count >= 6  # 6 篇默认文档，长文可能切片更多
    await kb.ensure_seeded()  # 再跑一遍不重复导入
    assert await kb.doc_count_async() == count


async def test_kb_long_document_is_chunked(kb):
    long_doc = "。".join(f"这是第 {i} 句关于配送时效的说明" for i in range(60))
    n = await kb.add_documents_async([{"title": "长文档", "content": long_doc}])
    assert n > 1  # 超过 500 字被切成多片
