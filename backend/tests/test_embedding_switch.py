"""
换 Embedding 模型这件事的安全网。

本地 n-gram 向量和远端模型的向量不在同一个空间里，混在一起比不会报错，只会让
语义检索返回和问题无关的片段。这里钉住四件事：
  - 远端失败就是失败，不拿本地向量顶上；过了冷却期自动恢复；
  - 失败时各处怎么降级：检索只走关键词、导入不落库、意图识别整体改用本地向量；
  - 库里的向量是谁算的有记录，和当前配置对不上就不启动；
  - scripts/reembed.py 原地重建向量，要么全换要么不动。

全部离线：远端 Embedding 是接在 Embedder 的 HTTP 客户端上的假端点。
"""
import argparse
import importlib.util
import json
import os
import pathlib
import secrets
from urllib.parse import urlsplit, urlunsplit

import pytest

from app.core import embedder as embedder_module
from app.core.embedder import Embedder, EmbeddingUnavailable
from app.core.intent_recognizer import _TEMPLATES, IntentRecognizer
from app.storage.vector_store import (
    EMBEDDING_SIGNATURE_KEY,
    LocalStore,
    check_embedding_signature,
)
from app.tools.knowledge_base import KnowledgeBase

TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()
REMOTE = {"base_url": "https://embeddings.example/v1", "api_key": "sk-test", "model": "fake-embed"}


class _FakeEmbeddingAPI:
    """OpenAI 兼容的 /embeddings：向量由文本算出（确定性），和本地 256 维向量不是一回事。"""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.fail = False
        self.requests: list[list[str]] = []

    def vector(self, text: str) -> list[float]:
        return Embedder.local_embedding(f"remote::{text}", self.dim)

    def handle(self, request):
        httpx = embedder_module.httpx
        texts = json.loads(request.content)["input"]
        self.requests.append(texts)
        if self.fail:
            return httpx.Response(503, json={"error": "upstream down"})
        data = [{"index": i, "embedding": self.vector(t)} for i, t in enumerate(texts)]
        # 故意倒着返回：对应关系要看 index，不能看先后
        return httpx.Response(200, json={"data": list(reversed(data))})


@pytest.fixture
def api(monkeypatch) -> _FakeEmbeddingAPI:
    fake = _FakeEmbeddingAPI()
    httpx = embedder_module.httpx
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake.handle), **kwargs),
    )
    return fake


def _remote(api: _FakeEmbeddingAPI, **kwargs) -> Embedder:
    return Embedder(**REMOTE, dim=api.dim, **kwargs)


# ── Embedder ──────────────────────────────────────────────────────────────────

async def test_remote_embedding_batches_and_keeps_input_order(api):
    emb = _remote(api, batch_size=2)
    texts = ["补件", "交期", "保修", "送货", "安装"]

    vectors = await emb.embed_many(texts)

    assert vectors == [api.vector(t) for t in texts]
    assert [len(batch) for batch in api.requests] == [2, 2, 1]
    assert await emb.embed("补件") == api.vector("补件")


async def test_remote_failure_raises_instead_of_substituting_local_vectors(api):
    """
    以前失败一次就永久改用本地哈希向量，维度沿用远端的——对得上维度所以不报错，
    算出来的余弦和内容无关。现在失败就抛出来，由调用方决定怎么降级。
    """
    emb = _remote(api)
    api.fail = True

    with pytest.raises(EmbeddingUnavailable):
        await emb.embed("补件")
    assert emb.backend == "remote"       # 走哪条路由配置决定，不因为一次失败改变
    assert emb.available is False


async def test_cooldown_skips_the_network_then_recovers_by_itself(api):
    emb = _remote(api)
    api.fail = True
    with pytest.raises(EmbeddingUnavailable):
        await emb.embed("补件")
    assert len(api.requests) == 1

    api.fail = False
    with pytest.raises(EmbeddingUnavailable):
        await emb.embed("补件")
    assert len(api.requests) == 1        # 冷却期内不再发请求：别把超时叠到每次调用上

    emb._down_until = 0.0                # 冷却期过了
    assert await emb.embed("补件") == api.vector("补件")
    assert emb.available is True


async def test_wrong_dimension_is_a_failure_not_a_silent_mismatch(api):
    emb = Embedder(**REMOTE, dim=api.dim + 1)   # EMBEDDING_DIM 配错了
    with pytest.raises(EmbeddingUnavailable, match="维度"):
        await emb.embed("补件")


async def test_local_embedder_never_touches_the_network(api):
    emb = Embedder()
    vectors = await emb.embed_many(["补件", "交期"])
    assert [len(v) for v in vectors] == [256, 256]
    assert api.requests == [] and emb.available is True


def test_signature_names_the_vector_space():
    assert Embedder().signature == "local_ngram/256"
    assert Embedder(**REMOTE, dim=1024).signature == "fake-embed/1024"
    assert Embedder(**REMOTE, dim=1024).status() == {
        "backend": "remote", "model": "fake-embed", "dim": 1024, "available": True,
    }


# ── 查询前缀：只加在检索查询上 ────────────────────────────────────────────────

QUERY_PREFIX = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"


async def test_query_prefix_goes_on_queries_and_never_on_documents(api, tmp_path):
    """
    区分查询和文档的模型（Qwen3-Embedding 这一类）：文档原样编，查询前面加任务说明。
    文档这一侧不能带上它——那样改一次前缀就得把全库的向量重算一遍。
    """
    local = LocalStore(db_path=str(tmp_path / "kb.db"))
    kb = KnowledgeBase(store=local, embedder=_remote(api, query_prefix=QUERY_PREFIX))

    await kb.add_documents_async([{"title": "安装调整", "content": "柜门关不严绝大多数是铰链未调到位"}])
    await kb.search_async("铰链怎么调", top_k=1)
    await local.close()

    assert api.requests == [
        ["安装调整\n柜门关不严绝大多数是铰链未调到位"],
        [QUERY_PREFIX + "铰链怎么调"],
    ]


def test_query_prefix_does_not_change_which_vectors_are_stored():
    with_prefix = Embedder(**REMOTE, dim=1024, query_prefix=QUERY_PREFIX)
    assert with_prefix.signature == Embedder(**REMOTE, dim=1024).signature


async def test_local_vectors_ignore_the_query_prefix():
    emb = Embedder(query_prefix=QUERY_PREFIX)
    assert await emb.embed_query("补件") == await emb.embed("补件")


def test_query_prefix_line_break_is_written_as_backslash_n_in_config(monkeypatch):
    for key, value in {"EMBEDDING_BASE_URL": REMOTE["base_url"], "EMBEDDING_API_KEY": REMOTE["api_key"],
                       "EMBEDDING_QUERY_PREFIX": "Instruct: find passages\\nQuery:"}.items():
        monkeypatch.setenv(key, value)
    assert Embedder.from_env()._query_prefix == "Instruct: find passages\nQuery:"


# ── 知识库：Embedding 不可用时怎么降级 ────────────────────────────────────────

@pytest.fixture
async def store(tmp_path):
    local = LocalStore(db_path=str(tmp_path / "kb.db"))
    yield local
    await local.close()


DOCS = [
    {"title": "安装调整", "content": "柜门关不严绝大多数是铰链未调到位"},
    {"title": "送货预约", "content": "送货需提前预约并具备卸货条件"},
]


async def test_search_falls_back_to_keywords_when_embedding_is_down(api, store):
    kb = KnowledgeBase(store=store, embedder=_remote(api))
    await kb.add_documents_async(DOCS)

    api.fail = True
    results = await kb.search_async("铰链怎么调", top_k=3)

    assert [r["title"] for r in results] == ["安装调整"]
    assert results[0]["retrieval"] == "lexical"
    assert results[0]["score"] is None    # 没有查询向量，就没有余弦分；不编一个出来


async def test_import_writes_nothing_when_embedding_is_down(api, store):
    """没有向量的分片进了库，稠密那一路永远召不回它；写进别的空间的向量更糟。"""
    kb = KnowledgeBase(store=store, embedder=_remote(api))
    api.fail = True

    with pytest.raises(EmbeddingUnavailable):
        await kb.import_documents(DOCS)
    assert await kb.doc_count_async() == 0
    assert (await kb.document_stats())["total_documents"] == 0

    api.fail = False
    kb._embedder._down_until = 0.0
    assert await kb.add_documents_async(DOCS) == 2    # 恢复后原样再导一次即可


async def test_empty_knowledge_base_does_not_block_startup_when_embedding_is_down(api, store):
    kb = KnowledgeBase(store=store, embedder=_remote(api))
    api.fail = True
    await kb.ensure_seeded()              # 不抛：为几篇示例文档让服务起不来不值得
    assert await kb.doc_count_async() == 0


async def test_one_document_is_embedded_in_one_batch(api, store):
    kb = KnowledgeBase(store=store, embedder=_remote(api))
    long_doc = "。".join(f"第 {i} 条规定的内容是这样的，写得足够长才会被切成好几片" * 6 for i in range(8))

    chunks = await kb.add_documents_async([{"title": "长文档", "content": long_doc}])

    assert chunks > 1
    assert len(api.requests) == 1 and len(api.requests[0]) == chunks


# ── 向量来源：换了模型没重建，不启动 ──────────────────────────────────────────

async def test_fresh_store_records_who_made_its_vectors(store):
    await check_embedding_signature(store, Embedder())
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) == "local_ngram/256"
    await check_embedding_signature(store, Embedder())    # 再启动一次：对得上，放行


async def test_startup_refuses_vectors_from_another_model(store):
    await KnowledgeBase(store=store, embedder=Embedder()).add_documents_async(DOCS)
    await check_embedding_signature(store, Embedder())

    # 维度相同、模型不同：列类型看不出来，余弦也算得出来，只是和内容无关
    with pytest.raises(RuntimeError, match="reembed.py"):
        await check_embedding_signature(store, Embedder(**REMOTE, dim=256))


async def test_store_without_a_record_is_adopted_not_blocked(store):
    """加这道检查之前建的库没有记录，不能因此起不来。"""
    await KnowledgeBase(store=store, embedder=Embedder()).add_documents_async(DOCS)
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) is None

    await check_embedding_signature(store, Embedder())
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) == "local_ngram/256"


# ── 整批换向量：要么全换，要么不动 ────────────────────────────────────────────

async def _seeded(store, embedder: Embedder) -> KnowledgeBase:
    kb = KnowledgeBase(store=store, embedder=embedder)
    await kb.add_documents_async(DOCS)
    await store.episodic_add("e1", "u1", "c1", "客户问过补件进度", "", await embedder.embed("x"))
    return kb


async def _replace_contract(store, api: _FakeEmbeddingAPI) -> None:
    """两个后端都要过的同一组断言。"""
    await _seeded(store, Embedder())
    before = await store.vector_state()
    assert before["kb_chunks"] == {"rows": 2, "dim": 256}
    assert before["memory_episodic"] == {"rows": 1, "dim": 256}
    revision = await store.kb_revision()

    kb_rows = await store.kb_embedding_rows()
    episodic_rows = await store.episodic_embedding_rows()
    new_kb = {r["id"]: api.vector(KnowledgeBase.embedding_text(r["title"], r["content"]))
              for r in kb_rows}
    new_episodic = {r["id"]: api.vector(r["summary"]) for r in episodic_rows}

    # 少一行的新向量：整个不换，旧向量和旧记录原样留着
    partial = dict(list(new_kb.items())[:1])
    with pytest.raises(RuntimeError, match="没有对应的新向量"):
        await store.replace_embeddings(api.dim, partial, new_episodic, "fake-embed/8")
    assert await store.vector_state() == before
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) is None
    assert await store.kb_revision() == revision        # 没换成，缓存着的检索结果照样能用

    await store.replace_embeddings(api.dim, new_kb, new_episodic, "fake-embed/8")

    after = await store.vector_state()
    assert after["kb_chunks"] == {"rows": 2, "dim": api.dim}
    assert after["memory_episodic"] == {"rows": 1, "dim": api.dim}
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) == "fake-embed/8"
    assert await store.kb_revision() not in ("", revision)  # 换成了：之前的检索结果作废
    # 换的只是向量：正文还在，新向量下照样能检索、能写入
    hits = await store.kb_search(api.vector("安装调整\n柜门关不严绝大多数是铰链未调到位"), 2)
    assert hits[0]["title"] == "安装调整" and hits[0]["score"] > 0.99
    assert await store.episodic_search("u1", api.vector("客户问过补件进度"), 3) == ["客户问过补件进度"]
    await store.episodic_add("e2", "u1", "c2", "新的记忆", "", api.vector("新的记忆"))


async def test_local_store_replaces_all_vectors_or_none(store):
    await _replace_contract(store, _FakeEmbeddingAPI())


@pytest.fixture
async def pg_store():
    """
    临时 schema 里的一套空表。换向量会改列的维度，不能在别的用例共用的表上做。
    """
    import asyncpg

    from app.storage.pg import Database
    from app.storage.vector_store import PgStore

    schema = f"test_{secrets.token_hex(4)}"
    conn = await asyncpg.connect(TEST_PG_DSN)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.close()

    parts = urlsplit(TEST_PG_DSN)
    scoped = urlunsplit(parts._replace(
        query="&".join(filter(None, [parts.query, f"search_path={schema}%2Cpublic"]))
    ))
    db = Database(scoped, max_size=2)
    await db.connect(embedding_dim=Embedder().dim)
    try:
        yield PgStore(db)
    finally:
        await db.close()
        conn = await asyncpg.connect(TEST_PG_DSN)
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_pg_store_replaces_all_vectors_or_none(pg_store):
    await _replace_contract(pg_store, _FakeEmbeddingAPI())


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_pg_lexical_search_works_without_a_query_vector(pg_store):
    await KnowledgeBase(store=pg_store, embedder=Embedder()).add_documents_async(DOCS)
    from app.core.lexical import query_tokens

    hits = await pg_store.kb_search_lexical(query_tokens("铰链"), None, 5)
    assert [h["title"] for h in hits] == ["安装调整"]
    assert hits[0]["score"] is None


# ── scripts/reembed.py ────────────────────────────────────────────────────────

def _load_script(name: str):
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_reembed_script_plans_first_and_swaps_only_on_apply(api, tmp_path, monkeypatch, capsys):
    db_path = str(tmp_path / "vec.db")
    store = LocalStore(db_path=db_path)
    await _seeded(store, Embedder())
    await check_embedding_signature(store, Embedder())
    await store.close()

    for key, value in {"EMBEDDING_BASE_URL": REMOTE["base_url"], "EMBEDDING_API_KEY": REMOTE["api_key"],
                       "EMBEDDING_MODEL": REMOTE["model"], "EMBEDDING_DIM": str(api.dim)}.items():
        monkeypatch.setenv(key, value)
    reembed = _load_script("reembed")
    args = argparse.Namespace(apply=False, dsn="", sqlite=db_path)

    assert await reembed.main(args) == 0
    plan = capsys.readouterr().out
    assert "local_ngram/256" in plan and "fake-embed/8" in plan and "加 --apply 执行" in plan
    assert len(api.requests) == 1        # 只探了一次维度，没有去算全库

    store = LocalStore(db_path=db_path)
    assert (await store.vector_state())["kb_chunks"]["dim"] == 256    # 计划阶段库没有动
    await store.close()

    args.apply = True
    assert await reembed.main(args) == 0

    store = LocalStore(db_path=db_path)
    assert (await store.vector_state())["kb_chunks"] == {"rows": 2, "dim": api.dim}
    assert (await store.vector_state())["memory_episodic"] == {"rows": 1, "dim": api.dim}
    await check_embedding_signature(store, Embedder.from_env())      # 换完之后服务起得来
    kb = KnowledgeBase(store=store, embedder=Embedder.from_env())
    assert (await kb.search_async("铰链未调到位", top_k=1))[0]["title"] == "安装调整"
    await store.close()

    capsys.readouterr()
    assert await reembed.main(args) == 0  # 再跑一次：已经一致，什么都不做
    assert "不需要重建" in capsys.readouterr().out


async def test_reembed_script_uses_the_embedding_saved_in_the_console(api, tmp_path, monkeypatch, capsys):
    """控制台保存过 Embedding，服务用的是那一组；脚本也得用它，不然重建完服务反而起不来。"""
    from app.core import model_settings

    db_path = str(tmp_path / "vec.db")
    store = LocalStore(db_path=db_path)
    await _seeded(store, Embedder())
    await store.close()

    for key in ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_DIM"):
        monkeypatch.delenv(key, raising=False)
    settings = str(tmp_path / "runtime" / "model_settings.json")
    monkeypatch.setenv("AI_HELPDESK_MODEL_SETTINGS_PATH", settings)
    model_settings.save_section(settings, "embedding", {**REMOTE, "dim": api.dim, "query_prefix": ""})
    reembed = _load_script("reembed")

    assert await reembed.main(argparse.Namespace(apply=True, dsn="", sqlite=db_path)) == 0
    assert "控制台保存的那一组" in capsys.readouterr().out
    store = LocalStore(db_path=db_path)
    assert (await store.vector_state())["kb_chunks"] == {"rows": 2, "dim": api.dim}
    assert await store.meta_get(EMBEDDING_SIGNATURE_KEY) == f"fake-embed/{api.dim}"
    await store.close()


async def test_reembed_script_leaves_the_store_alone_when_embedding_fails(api, tmp_path, monkeypatch, capsys):
    db_path = str(tmp_path / "vec.db")
    store = LocalStore(db_path=db_path)
    await _seeded(store, Embedder())
    await store.close()

    for key, value in {"EMBEDDING_BASE_URL": REMOTE["base_url"], "EMBEDDING_API_KEY": REMOTE["api_key"],
                       "EMBEDDING_MODEL": REMOTE["model"], "EMBEDDING_DIM": str(api.dim)}.items():
        monkeypatch.setenv(key, value)
    api.fail = True
    reembed = _load_script("reembed")

    assert await reembed.main(argparse.Namespace(apply=True, dsn="", sqlite=db_path)) == 1
    assert "库没有任何改动" in capsys.readouterr().out

    store = LocalStore(db_path=db_path)
    assert (await store.vector_state())["kb_chunks"]["dim"] == 256
    await store.close()


# ── 意图识别的 Embedding 一路 ─────────────────────────────────────────────────

async def test_intent_templates_are_embedded_in_batches(api):
    """几十条模板一条一个请求，第一条消息要等十几秒。"""
    recog = IntentRecognizer(api_key="k", embedder=_remote(api, batch_size=10))

    result = await recog._embedding_recognize("补件什么时候到")

    templates = sum(len(v) for v in _TEMPLATES.values())
    assert sum(len(batch) for batch in api.requests) == templates + 1
    assert len(api.requests) <= templates // 10 + 2
    assert recog._tpl_space.startswith("remote") and result["confidence"] > 0


async def test_intent_never_compares_vectors_from_two_spaces(api):
    """
    模板是远端向量、消息是本地向量（或者反过来）时，余弦算得出来但没有意义。
    远端中途挂掉，这一路整体改用本地向量；恢复之后整体换回去。
    """
    emb = _remote(api)
    recog = IntentRecognizer(api_key="k", embedder=emb)
    await recog._embedding_recognize("补件什么时候到")
    assert {len(v) for vecs in recog._tpl_embeddings.values() for v in vecs} == {api.dim}

    api.fail = True
    emb._down_until = 0.0
    result = await recog._embedding_recognize("补件什么时候到")

    assert recog.embedding_backend == "local_ngram"
    assert {len(v) for vecs in recog._tpl_embeddings.values() for v in vecs} == {256}
    assert result["intent"].value == "missing_part" and result["confidence"] > 0.3

    api.fail = False
    emb._down_until = 0.0
    await recog._embedding_recognize("补件什么时候到")
    assert recog.embedding_backend == "remote"
    assert {len(v) for vecs in recog._tpl_embeddings.values() for v in vecs} == {api.dim}


async def test_intent_falls_back_cleanly_when_remote_dies_mid_template_load(api):
    emb = _remote(api)
    recog = IntentRecognizer(api_key="k", embedder=emb)
    real_handle = api.handle

    def handle(request):
        # 消息那一次成功，接着加载模板时挂掉
        api.fail = len(api.requests) >= 1
        return real_handle(request)

    api.handle = handle
    result = await recog._embedding_recognize("补件什么时候到")

    assert recog._tpl_space == "local"
    assert {len(v) for vecs in recog._tpl_embeddings.values() for v in vecs} == {256}
    assert result["intent"].value == "missing_part"
