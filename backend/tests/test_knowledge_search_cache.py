"""
知识库检索缓存的作废：导入文档、换向量之后，缓存着的检索结果不能再用。

ToolManager 的缓存记着写入时的数据版本（Tool.cache_version），版本变了就不认；
knowledge_search 的版本由库里的分片表版本号和这个实例的 embedder.epoch 拼成。
员工和客户两个实例共用一个库，下面用两个 LocalStore 指向同一个文件来模拟
"一边导入了文档，另一边进程里的缓存也得作废"。
"""
import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

from app.core.embedder import Embedder
from app.core.lexical import index_text
from app.storage.vector_store import LocalStore
from app.tools.knowledge_base import KnowledgeBase
from app.tools.tool_manager import Tool, ToolManager

TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()

QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
    "required": ["query"],
}
HINGE = {"title": "铰链调整", "content": "柜门关不严时，先用十字螺丝刀调铰链上的深度螺丝。"}
DELIVERY = {"title": "送货安排", "content": "送货前一天电话确认时间，大件需要两人搬运。"}


# ── ToolManager：缓存认版本 ──────────────────────────────────────────────────

class _VersionedSearch:
    """按版本缓存的假检索工具：记下真正执行了几次，版本由用例自己改。"""

    def __init__(self, versioned: bool = True):
        self.version: str | Exception = "v1"
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.versioned = versioned

    async def handler(self, params, context):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return [{"content": f"{params['query']} 第 {self.calls} 次"}]

    async def current_version(self) -> str:
        if isinstance(self.version, Exception):
            raise self.version
        return self.version

    def manager(self) -> ToolManager:
        manager = ToolManager(api_key="test-key")
        manager.register(Tool(
            name="search", description="", handler=self.handler, schema=QUERY_SCHEMA,
            cache_ttl=300.0, cache_version=self.current_version if self.versioned else None,
        ))
        return manager


async def test_cached_result_is_reused_while_the_version_stays():
    search = _VersionedSearch()
    manager = search.manager()

    first = await manager.call("search", {"query": "铰链"})
    second = await manager.call("search", {"query": "铰链"})

    assert (first.cached, second.cached) == (False, True)
    assert second.data == first.data and search.calls == 1


async def test_a_tool_without_cache_version_still_caches_by_ttl():
    search = _VersionedSearch(versioned=False)
    manager = search.manager()

    await manager.call("search", {"query": "铰链"})
    assert (await manager.call("search", {"query": "铰链"})).cached
    assert search.calls == 1


async def test_a_new_version_retires_the_cached_result():
    search = _VersionedSearch()
    manager = search.manager()
    await manager.call("search", {"query": "铰链"})

    search.version = "v2"
    fresh = await manager.call("search", {"query": "铰链"})
    again = await manager.call("search", {"query": "铰链"})

    assert not fresh.cached and search.calls == 2
    assert again.cached and again.data == fresh.data      # 新版本下照样缓存


async def test_a_result_computed_across_a_version_change_is_not_served_later():
    """执行到一半数据被改了：这次的结果记在旧版本底下，之后的调用不会拿到它。"""
    search = _VersionedSearch()
    manager = search.manager()
    search.gate = asyncio.Event()

    pending = asyncio.create_task(manager.call("search", {"query": "铰链"}))
    while search.calls == 0:
        await asyncio.sleep(0)
    search.version = "v2"                                   # 执行期间有人导入了文档
    search.gate.set()
    assert (await pending).success
    search.gate = None

    later = await manager.call("search", {"query": "铰链"})
    assert not later.cached and search.calls == 2


async def test_an_unreadable_version_bypasses_the_cache():
    search = _VersionedSearch()
    manager = search.manager()
    search.version = ConnectionError("库连不上")

    first = await manager.call("search", {"query": "铰链"})
    second = await manager.call("search", {"query": "铰链"})

    assert first.success and second.success                 # 调用本身照常走
    assert not second.cached and search.calls == 2
    assert manager._cache == {}                              # 也没写进缓存


# ── knowledge_search：两个实例共用一个库 ─────────────────────────────────────

@pytest.fixture
async def two_instances(tmp_path):
    """员工和客户两个实例：各自一套进程内组件（含缓存），共用同一个库文件。"""
    path = str(tmp_path / "kb.db")
    instances = []
    for _ in range(2):
        store = LocalStore(db_path=path)
        embedder = Embedder()
        kb = KnowledgeBase(store=store, embedder=embedder)
        tools = ToolManager(api_key="test-key")
        # 与 api/main.py 的注册一致
        tools.register(Tool(
            name="knowledge_search", description="", handler=kb.search_handler,
            schema=QUERY_SCHEMA, cache_ttl=300.0, cache_version=kb.cache_version,
        ))
        instances.append(SimpleNamespace(store=store, embedder=embedder, kb=kb, tools=tools))
    await instances[0].kb.import_documents([DELIVERY])
    yield instances
    for instance in instances:
        await instance.store.close()


async def test_a_document_imported_on_one_instance_retires_the_others_cache(two_instances):
    staff, customer = two_instances
    query = {"query": "柜门关不严怎么办", "top_k": 3}
    before = await customer.tools.call("knowledge_search", query)
    assert HINGE["title"] not in [hit["title"] for hit in before.data]
    assert (await customer.tools.call("knowledge_search", query)).cached

    await staff.kb.import_documents([HINGE])

    after = await customer.tools.call("knowledge_search", query)
    assert not after.cached
    assert after.data[0]["title"] == HINGE["title"]


async def test_a_duplicate_import_keeps_the_cache(two_instances):
    """重复导入什么都没写，缓存着的结果照样能用。"""
    staff, customer = two_instances
    query = {"query": "送货", "top_k": 3}
    await customer.tools.call("knowledge_search", query)

    results = await staff.kb.import_documents([DELIVERY])

    assert [r["status"] for r in results] == ["duplicate"]
    assert (await customer.tools.call("knowledge_search", query)).cached


async def test_vectors_replaced_by_another_instance_retire_cached_results(two_instances):
    """另一个实例换完了向量（控制台换模型提交的那一步）：这边缓存着的结果不能再用。"""
    staff, customer = two_instances
    query = {"query": "送货", "top_k": 3}
    await customer.tools.call("knowledge_search", query)

    rows = await staff.store.kb_embedding_rows()
    vectors = {
        r["id"]: await staff.embedder.embed(KnowledgeBase.embedding_text(r["title"], r["content"]))
        for r in rows
    }
    await staff.store.replace_embeddings(staff.embedder.dim, vectors, {}, "local/256")

    assert not (await customer.tools.call("knowledge_search", query)).cached


async def test_adopting_a_new_embedder_retires_cached_results(two_instances):
    """这个实例跟上了新的向量模型（embedder.adopt）：之前按旧模型算的结果作废。"""
    _, customer = two_instances
    query = {"query": "送货", "top_k": 3}
    await customer.tools.call("knowledge_search", query)
    assert (await customer.tools.call("knowledge_search", query)).cached

    customer.embedder.adopt(Embedder())

    assert not (await customer.tools.call("knowledge_search", query)).cached


async def test_changing_a_category_keeps_the_cache(two_instances):
    """改分类只改文档表，检索结果不变，缓存不用作废。"""
    staff, customer = two_instances
    query = {"query": "送货", "top_k": 3}
    hit = (await customer.tools.call("knowledge_search", query)).data[0]

    await staff.kb.set_document_category(hit["doc_id"], "delivery")

    assert (await customer.tools.call("knowledge_search", query)).cached


# ── 存储层：分片表的版本号（两个后端同一组断言）─────────────────────────────

async def _revision_contract(store, embedder):
    """kb_chunks 每改一次版本号就换；只动文档表不换。"""
    chunk_id = f"rev-{uuid.uuid4().hex[:8]}"
    doc_id = f"doc_rev_{uuid.uuid4().hex[:8]}"
    revisions = [await store.kb_revision()]

    async def changed() -> bool:
        revisions.append(await store.kb_revision())
        return bool(revisions[-1]) and revisions[-1] != revisions[-2]

    await store.kb_add([{
        "id": chunk_id, "title": "铰链", "content": "铰链调整说明", "chunk_index": 0,
        "total_chunks": 1, "ingested_at": 1.0, "embedding": await embedder.embed("铰链调整说明"),
    }])
    assert await changed()
    await store.kb_set_tokens([(chunk_id, index_text("铰链", "铰链调整说明"))])
    assert await changed()
    await store.kb_assign_doc([chunk_id], doc_id)
    assert await changed()

    await store.kb_doc_add({
        "id": doc_id, "title": "铰链", "content": "铰链调整说明", "content_hash": uuid.uuid4().hex,
        "origin": "original", "source_type": "manual", "source_name": "", "chunk_count": 1,
        "created_at": 1.0, "category": "",
    })
    await store.kb_doc_update(doc_id, {"category": "delivery"})
    assert not await changed()

    await store.kb_doc_delete(doc_id)                     # 连同名下的分片一起删
    assert await changed()


async def test_local_store_revision_moves_with_every_chunk_write(tmp_path):
    store = LocalStore(db_path=str(tmp_path / "vec.db"))
    assert await store.kb_revision() == ""                   # 还没写过分片
    await _revision_contract(store, Embedder())
    await store.close()


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_pg_store_revision_moves_with_every_chunk_write():
    from app.storage.pg import Database
    from app.storage.vector_store import PgStore

    embedder = Embedder()
    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=embedder.dim)
    try:
        await _revision_contract(PgStore(db), embedder)
    finally:
        await db.close()
