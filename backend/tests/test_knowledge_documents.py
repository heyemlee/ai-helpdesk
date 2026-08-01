"""知识库的文档模型：导入、浏览、历史分片归档。

和 test_vector_storage.py 一样，同一组断言在两个后端上各跑一遍：LocalStore
直接测，PgStore 在设置了 TEST_POSTGRES_DSN 时测。文档表是给"知识库里到底有
什么"作答的——两个后端答得不一样，这个问题就等于没答。

断言只针对本次用例自己写进去的文档（标题里带随机后缀）：PgStore 可能指向
一个已经有数据的开发库。
"""
import asyncio
import hashlib
import os
import sqlite3
import time
import uuid

import pytest

from app.core.embedder import Embedder
from app.core.lexical import index_text
from app.storage.vector_store import LocalStore, like_pattern
from app.tools import knowledge_base as kbmod
from app.tools.knowledge_base import (
    KnowledgeBase,
    document_excerpt,
    document_hash,
    document_heading,
    match_snippet,
    split_heading,
)

TEST_PG_DSN = os.getenv("TEST_POSTGRES_DSN", "").strip()

BACKENDS = ["sqlite"] + (["postgres"] if TEST_PG_DSN else [])


@pytest.fixture(params=BACKENDS)
async def store(request, tmp_path):
    if request.param == "sqlite":
        local = LocalStore(db_path=str(tmp_path / "kb.db"))
        yield local
        await local.close()
        return
    from app.storage.pg import Database
    from app.storage.vector_store import PgStore

    db = Database(TEST_PG_DSN)
    await db.connect(embedding_dim=Embedder().dim)
    try:
        yield PgStore(db)
    finally:
        await db.close()


@pytest.fixture
def kb(store):
    return KnowledgeBase(store=store, embedder=Embedder())


def uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def legacy_chunks(title: str, content: str, ingested_at: float) -> list[dict]:
    """按加文档表之前的写法造分片：id 是 md5(标题 + 片段)，没有 doc_id。"""
    embedder = Embedder()
    chunks = KnowledgeBase._chunk_text(content, chunk_size=500)
    return [
        {
            "id": hashlib.md5(f"{title}\n{chunk}".encode()).hexdigest(),
            "title": title,
            "content": chunk,
            "chunk_index": i,
            "total_chunks": len(chunks),
            "ingested_at": ingested_at,
            "embedding": await embedder.embed(f"{title}\n{chunk}"),
            "tokens": index_text(title, chunk),
        }
        for i, chunk in enumerate(chunks)
    ]


LONG_DOC = "\n".join(
    f"Step {i}: check the approved shop drawing before releasing work order {i} to the panel shop."
    for i in range(1, 16)
)


# ── 纯函数 ────────────────────────────────────────────────────────────────────

def test_heading_and_excerpt_come_from_the_document_itself():
    content = "# US Lab Warranty Review\n\n模拟售后规则：保修先查 **written limited warranty**。\n"
    assert document_heading(content) == "US Lab Warranty Review"
    excerpt = document_excerpt(content)
    assert excerpt.startswith("模拟售后规则")
    assert "#" not in excerpt and "*" not in excerpt
    # 没有标题行的文档不硬造一个
    assert document_heading("第一行就是正文。\n# 后面才出现的标题") == ""
    # 下划线是字段名的一部分，不是强调号：剥掉的话 on_hand_qty 就成了另一个词
    assert document_excerpt("可用量=`on_hand_qty`-locked_qty\n> 按 **material.unit** 计") \
        == "可用量= on_hand_qty -locked_qty 按 material.unit 计"


def test_flattened_legacy_heading_is_split_at_the_lost_line_break():
    """
    老切片逻辑把换行换成了句号：超过一片长度的文档，标题和正文被压在同一行。
    这时标题只取到第一个句号——不然整篇正文都会被当成标题显示。
    """
    flat = "# US Lab Order Status and Delivery。模拟业务流程：approved quote、production。LTL freight is separate."
    assert split_heading(flat) == (
        "US Lab Order Status and Delivery",
        "模拟业务流程：approved quote、production。LTL freight is separate.",
    )
    assert document_excerpt(flat).startswith("模拟业务流程")
    # 一整段恰好以 # 开头的长文本不是标题
    long_line = "# " + "word " * 60
    assert split_heading(long_line) == ("", long_line)
    assert split_heading("没有标题\n第二行") == ("", "没有标题\n第二行")
    assert split_heading("\n\n## 二级标题 ##\n\n正文") == ("二级标题", "正文")


def test_excerpt_is_cut_not_summarised():
    text = "板材按张入库。" * 80
    excerpt = document_excerpt(text, limit=50)
    assert excerpt.endswith("…") and len(excerpt) <= 51
    assert excerpt.rstrip("…") in " ".join(text.split())


def test_hash_ignores_only_edge_whitespace():
    assert document_hash("t", "body\n") == document_hash(" t ", "body")
    assert document_hash("t", "a\r\nb") == document_hash("t", "a\nb")
    assert document_hash("t", "body") != document_hash("t", "body.")
    assert document_hash("t", "body") != document_hash("u", "body")


def test_snippet_shows_where_the_terms_were_found():
    content = ("# Order Status and Delivery\n\n" + "x" * 300
               + " LTL freight and installation are separately scheduled. Curbside delivery is extra. "
               + "y" * 300 + " A punch list records open items.")
    near = match_snippet(content, ["ltl", "delivery"])
    assert "LTL freight" in near and "Curbside delivery" in near    # 挨得近的并成一段
    assert near.startswith("…") and near.endswith("…")
    assert "#" not in near                       # 标题行不进片段，标题命中由标题高亮说明
    far = match_snippet(content, ["ltl", "punch"])
    assert "LTL freight" in far and "punch list" in far and " … " in far   # 离得远的给两段
    assert match_snippet(content, ["absent"]) == ""
    assert match_snippet("# Delivery\n\n正文里没有这个词。", ["delivery"]) == ""


def test_like_pattern_treats_wildcards_literally():
    assert like_pattern("50%_off") == "%50\\%\\_off%"


# ── 导入与浏览 ────────────────────────────────────────────────────────────────

async def test_import_records_the_document_and_its_source(kb):
    title = uniq("ltl")
    content = "# LTL Delivery Scope\n\nCurbside delivery does not include unloading.\n"
    [result] = await kb.import_documents(
        [{"title": title, "content": content}], source_type="upload", source_name="ltl.md"
    )
    assert result["status"] == "created" and result["chunks"] == 1

    doc = await kb.get_document(result["id"])
    assert doc["content"] == content                      # 原文原样存下，换行还在
    assert doc["title"] == title
    assert doc["display_title"] == "LTL Delivery Scope"   # 正文自己的标题行
    assert doc["origin"] == "original"
    assert (doc["source_type"], doc["source_name"]) == ("upload", "ltl.md")
    assert doc["chunk_count"] == doc["chunks"] == len(doc["passages"]) == 1
    assert doc["passages"][0]["indexed"] is True
    assert abs(doc["created_at"] - time.time()) < 60


async def test_reimport_is_idempotent(kb, store):
    title = uniq("policy")
    doc = {"title": title, "content": "缺件需凭装箱单登记。"}
    [first] = await kb.import_documents([doc])
    before = await store.kb_count()
    [again] = await kb.import_documents([doc])
    assert again == {"id": first["id"], "title": title, "status": "duplicate", "chunks": 0}
    assert await store.kb_count() == before
    assert await kb.add_documents_async([doc]) == 0   # 旧接口的口径不变：新增片段数


async def test_same_title_different_text_stays_two_documents(kb):
    """同名不同文是两篇。只看标题合并，会拼出一篇谁也没写过的文档。"""
    title = uniq("warranty")
    [v1] = await kb.import_documents([{"title": title, "content": "柜体质保 5 年。"}])
    [v2] = await kb.import_documents([{"title": title, "content": "柜体质保 10 年，五金 3 年。"}])
    assert v1["status"] == v2["status"] == "created"
    assert v1["id"] != v2["id"]

    listing = await kb.list_documents(query=title)
    assert {d["id"] for d in listing["documents"]} == {v1["id"], v2["id"]}
    assert (await kb.get_document(v1["id"]))["content"] == "柜体质保 5 年。"
    assert (await kb.get_document(v2["id"]))["content"] == "柜体质保 10 年，五金 3 年。"


async def test_duplicates_inside_one_batch_are_imported_once(kb):
    title = uniq("batch")
    doc = {"title": title, "content": "同一批里出现两次。"}
    first, second = await kb.import_documents([doc, dict(doc)])
    assert first["status"] == "created"
    assert second["status"] == "duplicate" and second["id"] == first["id"]


async def test_empty_document_is_reported_not_stored(kb):
    [result] = await kb.import_documents([{"title": uniq("blank"), "content": "  \n "}])
    assert result["status"] == "empty" and result["id"] is None


async def test_long_document_keeps_original_text_and_ordered_passages(kb):
    [result] = await kb.import_documents([{"title": uniq("sop"), "content": LONG_DOC}])
    assert result["chunks"] > 1
    doc = await kb.get_document(result["id"])
    assert doc["content"] == LONG_DOC                   # 正文不是分片拼的，是原文
    assert [p["index"] for p in doc["passages"]] == list(range(result["chunks"]))
    assert doc["chunk_count"] == doc["chunks"] == result["chunks"]
    assert doc["chars"] == len(LONG_DOC)


async def test_listing_counts_documents_and_passages_separately(kb):
    await kb.import_documents([{"title": uniq("one"), "content": "短文档。"}])
    await kb.import_documents([{"title": uniq("many"), "content": LONG_DOC}])
    listing = await kb.list_documents()
    assert listing["total_documents"] >= 2
    assert listing["total_chunks"] > listing["total_documents"]   # 一篇可以有多片
    assert listing["matched"] == listing["total_documents"]
    assert listing["query"] == ""
    assert all("content" not in d and "match" not in d for d in listing["documents"])


async def test_newest_documents_come_first(kb):
    older = (await kb.import_documents([{"title": uniq("older"), "content": "先导入。"}]))[0]
    newer = (await kb.import_documents([{"title": uniq("newer"), "content": "后导入。"}]))[0]
    ids = [d["id"] for d in (await kb.list_documents(limit=500))["documents"]]
    assert ids.index(newer["id"]) < ids.index(older["id"])


async def test_filter_requires_every_term_and_says_where_it_matched(kb):
    tag = uuid.uuid4().hex[:8]
    [freight] = await kb.import_documents([{
        "title": f"fulfillment-{tag}",
        "content": f"# Order Status {tag}\n\nLTL freight is scheduled separately. "
                   "Curbside delivery does not include installation.",
    }])
    [damage] = await kb.import_documents([{
        "title": f"damage-{tag}",
        "content": f"Freight damage {tag}: keep the delivery receipt and photos.",
    }])

    # 两个词不相邻，但都在同一篇里 → 命中；只含其中一个词的那篇不算
    both = await kb.list_documents(query=f"{tag} LTL delivery")
    assert [d["id"] for d in both["documents"]] == [freight["id"]]
    assert both["matched"] == 1
    match = both["documents"][0]["match"]
    assert match["title"] is False and "LTL freight" in match["snippet"]

    # 大小写不敏感；标题命中的排在正文命中的前面
    by_title = await kb.list_documents(query=f"DAMAGE-{tag}")
    assert [d["id"] for d in by_title["documents"]] == [damage["id"]]
    assert by_title["documents"][0]["match"]["title"] is True

    shared = await kb.list_documents(query=f"{tag} freight")
    assert {d["id"] for d in shared["documents"]} == {freight["id"], damage["id"]}

    nothing = await kb.list_documents(query=f"{tag} zzz-no-such-term")
    assert nothing["documents"] == [] and nothing["matched"] == 0
    assert nothing["total_documents"] >= 2     # 没匹配上 ≠ 库是空的


async def test_filter_matches_chinese_and_literal_wildcards(kb):
    tag = uuid.uuid4().hex[:8]
    [install] = await kb.import_documents([{
        "title": f"install-{tag}", "content": f"{tag} 仍异常时转人工安装复核，定金比例 50%_off。",
    }])
    assert [d["id"] for d in (await kb.list_documents(query=f"{tag} 安装"))["documents"]] \
        == [install["id"]]
    assert (await kb.list_documents(query=f"{tag} 50%_off"))["matched"] == 1
    # `%` 和 `_` 是字面量，不是通配符
    assert (await kb.list_documents(query=f"{tag} 50%xoff"))["matched"] == 0
    assert (await kb.list_documents(query=f"{tag} 5_%"))["matched"] == 0


async def test_listing_is_paged(kb):
    tag = uuid.uuid4().hex[:8]
    for i in range(3):
        await kb.import_documents([{"title": f"page-{tag}-{i}", "content": f"第 {i} 篇。"}])
    first = await kb.list_documents(query=f"page-{tag}", limit=2)
    rest = await kb.list_documents(query=f"page-{tag}", limit=2, offset=2)
    assert first["matched"] == rest["matched"] == 3
    assert len(first["documents"]) == 2 and len(rest["documents"]) == 1
    assert not {d["id"] for d in first["documents"]} & {d["id"] for d in rest["documents"]}


async def test_search_hits_point_back_to_their_document(kb):
    tag = uuid.uuid4().hex[:8]
    [doc] = await kb.import_documents([{
        "title": f"hinge-{tag}", "content": f"{tag} 柜门对不齐先调铰链，一次只调一个方向。",
    }])
    hits = await kb.search_async(f"{tag} 铰链", top_k=5)
    hit = next(h for h in hits if h["doc_id"] == doc["id"])
    passages = (await kb.get_document(doc["id"]))["passages"]
    assert hit["chunk_id"] == passages[0]["id"]
    assert list(hit)[-2:] == ["doc_id", "chunk_id"]   # 新字段排在最后，见 search_async 的说明
    assert (await kb.document_titles([doc["id"], "", "missing"])) == {doc["id"]: f"hinge-{tag}"}


async def test_unknown_document_is_none(kb):
    assert await kb.get_document("doc_does_not_exist") is None


# ── 历史分片归档 ──────────────────────────────────────────────────────────────

async def test_legacy_chunks_become_documents_without_touching_the_chunks(kb, store):
    title = uniq("legacy-single")
    content = "# Old Policy\n\n补件的到货日期以工厂排产确认为准。\n"
    [chunk] = await legacy_chunks(title, content, ingested_at=1_700_000_000.25)
    await store.kb_add([chunk])

    assert await kb.ensure_documents() >= 1
    [doc] = (await kb.list_documents(query=title))["documents"]
    assert doc["id"].startswith("legacy_")
    assert doc["origin"] == "legacy"
    assert (doc["source_type"], doc["source_name"]) == ("legacy", "")   # 当时没记，现在不补
    assert doc["created_at"] == 1_700_000_000.25                        # 分片自己的入库时间
    assert doc["display_title"] == "Old Policy"

    full = await kb.get_document(doc["id"])
    assert full["content"] == content
    assert [p["id"] for p in full["passages"]] == [chunk["id"]]   # 分片 id 没变，索引还是原来那条
    assert await kb.ensure_documents() == 0                        # 幂等


async def test_legacy_long_document_is_rebuilt_and_labelled(kb, store):
    title = uniq("legacy-long")
    chunks = await legacy_chunks(title, LONG_DOC, ingested_at=1_700_000_100.0)
    assert len(chunks) > 1
    await store.kb_add(chunks)
    await kb.ensure_documents()

    [doc] = (await kb.list_documents(query=title))["documents"]
    full = await kb.get_document(doc["id"])
    assert full["origin"] == "legacy"
    assert full["chunk_count"] == full["chunks"] == len(chunks)
    assert full["content"] == "\n\n".join(c["content"] for c in chunks)
    assert full["content"] != LONG_DOC      # 拼回来的不是原文——所以才要标出来


async def test_legacy_documents_are_grouped_by_import_not_by_title(kb, store):
    """同一个标题、两次导入、内容不同：是两篇历史文档。"""
    title = uniq("legacy-same-title")
    await store.kb_add(await legacy_chunks(title, "2025 版：柜体质保 5 年。", 1_700_000_200.0))
    await store.kb_add(await legacy_chunks(title, "2026 版：柜体质保 10 年。", 1_700_000_300.0))
    await kb.ensure_documents()

    docs = (await kb.list_documents(query=title))["documents"]
    assert len(docs) == 2
    contents = {(await kb.get_document(d["id"]))["content"] for d in docs}
    assert contents == {"2025 版：柜体质保 5 年。", "2026 版：柜体质保 10 年。"}


async def test_same_title_in_one_legacy_batch_is_not_merged(kb, store):
    """同一批里两篇同名文档：导入时间也一样，只能靠"序号重复"认出来。"""
    title = uniq("legacy-batch")
    at = 1_700_000_400.0
    await store.kb_add(
        await legacy_chunks(title, "甲：先量尺。", at) + await legacy_chunks(title, "乙：先出图。", at)
    )
    await kb.ensure_documents()
    docs = (await kb.list_documents(query=title))["documents"]
    assert len(docs) == 2
    assert {d["origin"] for d in docs} == {"legacy"}
    assert {(await kb.get_document(d["id"]))["content"] for d in docs} == {"甲：先量尺。", "乙：先出图。"}


async def test_incomplete_legacy_document_is_marked_partial(kb, store):
    title = uniq("legacy-partial")
    chunks = await legacy_chunks(title, LONG_DOC, ingested_at=1_700_000_500.0)
    await store.kb_add(chunks[1:])          # 第一片没了
    await kb.ensure_documents()

    [doc] = (await kb.list_documents(query=title))["documents"]
    assert doc["origin"] == "legacy_partial"
    assert doc["chunk_count"] == len(chunks)        # 当初切成了几片
    assert doc["chunks"] == len(chunks) - 1         # 现在找得到几片
    full = await kb.get_document(doc["id"])
    assert chunks[0]["content"] not in full["content"]


async def test_reimporting_a_legacy_document_does_not_duplicate_it(kb, store):
    # 短文档：拼回来的正文就是原文，哈希对得上
    short_title = uniq("legacy-short")
    short = "缺件需在验收单注明。"
    await store.kb_add(await legacy_chunks(short_title, short, 1_700_000_600.0))
    # 长文档：拼回来的不是原文，哈希对不上，靠逐片比对认出来
    long_title = uniq("legacy-reimport")
    await store.kb_add(await legacy_chunks(long_title, LONG_DOC, 1_700_000_700.0))
    await kb.ensure_documents()
    before = await store.kb_count()

    short_result, long_result = await kb.import_documents(
        [{"title": short_title, "content": short}, {"title": long_title, "content": LONG_DOC}],
        source_type="upload", source_name="reimport.json",
    )
    assert short_result["status"] == "duplicate"
    assert long_result["status"] == "restored"
    assert await store.kb_count() == before             # 一片都没多

    # 正文一字不差地又导了一遍：能确认存着的就是原文，不再标成"历史"
    confirmed = await kb.get_document(short_result["id"])
    assert confirmed["origin"] == "original" and confirmed["content"] == short
    assert confirmed["source_type"] == "legacy" and confirmed["created_at"] == 1_700_000_600.0

    restored = await kb.get_document(long_result["id"])
    assert restored["origin"] == "original" and restored["content"] == LONG_DOC
    assert restored["created_at"] == 1_700_000_700.0    # 第一次导入的时间
    assert restored["source_type"] == "legacy"          # 那次的来源没记，不拿这次的顶替
    # 补全之后再导一次，就是普通的重复
    [again] = await kb.import_documents([{"title": long_title, "content": LONG_DOC}])
    assert again["status"] == "duplicate" and again["id"] == long_result["id"]


async def test_first_import_after_upgrade_does_not_reimport_the_old_library(kb, store):
    """
    升级后第一次导入：文档表还是空的，历史分片还没归档。
    这时候导入老知识库里已有的文档，不能把它再写一遍。
    """
    title = uniq("legacy-first")
    content = "台面结构性开裂先暂停使用。"
    await store.kb_add(await legacy_chunks(title, content, 1_700_000_800.0))
    before = await store.kb_count()
    assert await kb.add_documents_async([{"title": title, "content": content}]) == 0
    assert await store.kb_count() == before
    assert (await kb.list_documents(query=title))["matched"] == 1


# ── 空壳记录 ──────────────────────────────────────────────────────────────────

async def test_store_reports_documents_that_lost_their_chunks(kb, store):
    [emptied] = await kb.import_documents([{"title": uniq("emptied"), "content": "分片会被清掉。"}])
    [kept] = await kb.import_documents([{"title": uniq("kept"), "content": "分片还在。"}])
    ids = [p["id"] for p in (await kb.get_document(emptied["id"]))["passages"]]
    await store.kb_assign_doc(ids, f"gone-{emptied['id']}")   # 模拟分片被直接清走

    now = time.time()
    shells = await store.kb_docs_without_chunks(now + 1)
    assert emptied["id"] in shells and kept["id"] not in shells
    # 截止时间之后才登记的不算——那是留给"正在导入"的余地
    assert emptied["id"] not in await store.kb_docs_without_chunks(now - 600)
    await store.kb_doc_delete(f"gone-{emptied['id']}")        # 收拾：把那几片也清掉


async def test_prune_respects_the_grace_period(kb, store, monkeypatch):
    [doc] = await kb.import_documents([{"title": uniq("shell"), "content": "先登记后清空。"}])
    ids = [p["id"] for p in (await kb.get_document(doc["id"]))["passages"]]
    await store.kb_assign_doc(ids, f"gone-{doc['id']}")

    await kb.prune_empty_documents()
    assert await kb.get_document(doc["id"]) is not None     # 刚登记的不动

    monkeypatch.setattr(kbmod, "EMPTY_DOC_GRACE_S", -60.0)
    assert await kb.prune_empty_documents() >= 1
    assert await kb.get_document(doc["id"]) is None
    await store.kb_doc_delete(f"gone-{doc['id']}")          # 收拾：把那几片也清掉


async def test_a_shell_without_chunks_does_not_block_reimport(kb, store):
    """
    记录还在、分片没了：再导同一篇必须真的导进去。
    认它是"已有"的话，这篇文档就再也检索不到，而列表里看着一切正常。
    """
    title = uniq("replaced")
    doc = {"title": title, "content": "按数据集版本整体替换知识库。"}
    [first] = await kb.import_documents([doc])
    ids = [p["id"] for p in (await kb.get_document(first["id"]))["passages"]]
    await store.kb_assign_doc(ids, f"gone-{first['id']}")
    await store.kb_doc_delete(f"gone-{first['id']}")          # 分片真的删掉，只剩文档记录
    assert (await kb.get_document(first["id"]))["chunks"] == 0

    [again] = await kb.import_documents([doc])
    assert again["status"] == "created" and again["chunks"] == 1
    assert again["id"] == first["id"]                         # ID 由内容决定，还是那一篇
    assert (await kb.get_document(again["id"]))["chunks"] == 1
    assert (await kb.list_documents(query=title))["matched"] == 1
    hits = await kb.search_async(f"{title} 替换知识库", top_k=5)
    assert any(h["doc_id"] == again["id"] for h in hits)      # 又检索得到了


# ── 并发导入 ──────────────────────────────────────────────────────────────────

async def test_same_document_imported_twice_at_once_lands_once(kb, store, monkeypatch):
    """
    两个实例同时导同一篇：各自都查过"库里没有"，然后各自去写。
    文档 ID 和分片 ID 都由内容决定，所以写的是同一批行——库里只有一份。
    """
    title = uniq("race")
    doc = {"title": title, "content": LONG_DOC}
    before = await store.kb_count()

    async def nobody_has_it(_digest):       # 把"先查后写"之间的那道缝撑开
        return None

    monkeypatch.setattr(store, "kb_doc_find_by_hash", nobody_has_it)
    [first], [second] = await asyncio.gather(kb.import_documents([doc]), kb.import_documents([doc]))

    assert first["id"] == second["id"]
    assert sorted([first["status"], second["status"]]) == ["created", "duplicate"]
    chunks = max(first["chunks"], second["chunks"])
    assert chunks > 1 and await store.kb_count() == before + chunks
    assert (await kb.list_documents(query=title))["matched"] == 1
    full = await kb.get_document(first["id"])
    assert full["chunks"] == full["chunk_count"] == chunks


async def test_two_instances_seeding_an_empty_library_do_not_double_it(tmp_path):
    """
    空库上两个实例一起启动（compose 里员工实例和客户实例就是这样），
    都看到"库是空的"，都去导默认文档。结果必须还是一套。
    """
    path = str(tmp_path / "shared.db")
    staff, customer = LocalStore(db_path=path), LocalStore(db_path=path)
    try:
        kbs = [KnowledgeBase(store=s, embedder=Embedder()) for s in (staff, customer)]
        await asyncio.gather(*(k.ensure_seeded() for k in kbs))
        listing = await kbs[0].list_documents()
        assert listing["total_documents"] == len(kbmod._DEFAULT_DOCS)
        assert listing["total_chunks"] == len(kbmod._DEFAULT_DOCS)   # 默认文档都是单片
        assert len({d["title"] for d in listing["documents"]}) == len(kbmod._DEFAULT_DOCS)
    finally:
        await staff.close()
        await customer.close()


async def test_passage_shared_by_two_versions_is_retrieved_once(kb):
    """
    同名文档的两个版本共用一段原文。两篇都保留，但检索结果里这段文字只出现一次，
    不然回答的上下文会被同一段话占掉两个名额。
    """
    tag = uuid.uuid4().hex[:8]
    title = f"hinge-{tag}"
    # 共用的那段刚好占满第一片：再接任何一句都会超过切片长度，所以两个版本的首片相同
    shared = "。".join([f"{tag} 柜门对不齐先调铰链一次只调一个方向每次半圈正常门缝上下一致"] * 12)
    old_tail = "旧版补充说明五金件质保三年自安装验收签字之日起算以正式质保卡为准不含人为损坏和正常磨损"
    new_tail = "新版补充说明五金件质保五年自安装验收签字之日起算以正式质保卡为准不含人为损坏和正常磨损"
    assert len(shared) <= kbmod.CHUNK_SIZE < len(shared) + len(old_tail) + 1
    v1 = shared + "。" + old_tail
    v2 = shared + "。" + new_tail
    [a], [b] = await kb.import_documents([{"title": title, "content": v1}]), \
        await kb.import_documents([{"title": title, "content": v2}])
    assert a["status"] == b["status"] == "created" and a["id"] != b["id"]

    first_a = (await kb.get_document(a["id"]))["passages"][0]["content"]
    first_b = (await kb.get_document(b["id"]))["passages"][0]["content"]
    assert first_a == first_b                                   # 两篇各存了一份相同的首片

    hits = await kb.search_async(f"{tag} 铰链 门缝", top_k=5)
    same = [h for h in hits if h["content"] == first_a]
    assert len(same) == 1
    assert len({(h["title"], h["content"]) for h in hits}) == len(hits)


# ── 老库升级（SQLite 文件）────────────────────────────────────────────────────

OLD_SCHEMA = """
CREATE TABLE kb_chunks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL DEFAULT 1,
    ingested_at  REAL NOT NULL,
    embedding    TEXT NOT NULL,
    tokens       TEXT NOT NULL DEFAULT ''
);
"""


async def test_existing_sqlite_file_is_upgraded_in_place(tmp_path):
    """一个加文档表之前的 vector_store.db：打开后原有分片和索引一行不少。"""
    import json

    path = tmp_path / "old.db"
    rows = await legacy_chunks("install", "# Door Troubleshooting\n\n先查柜体水平。", 1_759_617_315.0)
    rows += await legacy_chunks("stock", "库存可用量=on_hand_qty-locked_qty。", 1_759_617_315.0)
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.executemany(
            "INSERT INTO kb_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["id"], r["title"], r["content"], r["chunk_index"], r["total_chunks"],
              r["ingested_at"], json.dumps(r["embedding"]), r["tokens"]) for r in rows],
        )

    store = LocalStore(db_path=str(path))
    try:
        kb = KnowledgeBase(store=store, embedder=Embedder())
        assert await store.kb_count() == 2
        assert (await kb.list_documents())["total_documents"] == 0   # 还没归档
        assert await kb.ensure_documents() == 2

        listing = await kb.list_documents()
        assert listing["total_documents"] == listing["legacy_documents"] == 2
        assert listing["total_chunks"] == 2
        assert {d["display_title"] for d in listing["documents"]} == {"Door Troubleshooting", "stock"}
        # 同一时刻导入的按标题排，顺序稳定
        assert [d["title"] for d in listing["documents"]] == ["install", "stock"]

        # 检索照常，命中带着文档出处
        hits = await kb.search_async("柜体水平", top_k=2)
        assert hits[0]["title"] == "install" and hits[0]["doc_id"].startswith("legacy_")
        assert hits[0]["chunk_id"] == rows[0]["id"]
    finally:
        await store.close()

    # 再打开一次：不重复归档，文档 id 不变
    reopened = LocalStore(db_path=str(path))
    try:
        kb = KnowledgeBase(store=reopened, embedder=Embedder())
        assert await kb.ensure_documents() == 0
        assert {d["id"] for d in (await kb.list_documents())["documents"]} \
            == {d["id"] for d in listing["documents"]}
    finally:
        await reopened.close()


OLD_PG_SCHEMA = """
CREATE TABLE kb_chunks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL DEFAULT 1,
    ingested_at  DOUBLE PRECISION NOT NULL,
    embedding    vector(256) NOT NULL,
    tokens       tsvector
);
CREATE INDEX idx_kb_tokens ON kb_chunks USING GIN (tokens);
"""


@pytest.mark.skipif(not TEST_PG_DSN, reason="未设置 TEST_POSTGRES_DSN")
async def test_existing_postgres_table_is_upgraded_in_place():
    """
    一张加文档表之前的 kb_chunks（生产库就是这个样子）：连上之后补列、归档，
    原有分片、向量和关键词索引都还在。在一个临时 schema 里做，不碰别的表。
    """
    import asyncpg

    from app.storage.pg import Database
    from app.storage.vector_store import PgStore

    schema = f"upgrade_{uuid.uuid4().hex[:8]}"
    rows = await legacy_chunks("warranty", "# Warranty Review\n\n保修先查 written limited warranty。",
                               1_759_617_315.0)
    rows += await legacy_chunks("long", LONG_DOC, 1_759_617_316.0)

    raw = await asyncpg.connect(TEST_PG_DSN)
    try:
        await raw.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await raw.execute(f"CREATE SCHEMA {schema}")
        await raw.execute(f"SET search_path TO {schema}, public")
        await raw.execute(OLD_PG_SCHEMA)
        await raw.executemany(
            "INSERT INTO kb_chunks (id, title, content, chunk_index, total_chunks, ingested_at,"
            " embedding, tokens) VALUES ($1, $2, $3, $4, $5, $6, $7::vector, to_tsvector('simple', $8))",
            [(r["id"], r["title"], r["content"], r["chunk_index"], r["total_chunks"],
              r["ingested_at"], PgStore._vec(r["embedding"]), r["tokens"]) for r in rows],
        )

        joiner = "&" if "?" in TEST_PG_DSN else "?"
        db = Database(f"{TEST_PG_DSN}{joiner}search_path={schema},public")
        await db.connect(embedding_dim=256)
        try:
            store = PgStore(db)
            kb = KnowledgeBase(store=store, embedder=Embedder())
            assert await store.kb_count() == len(rows)
            assert await kb.ensure_documents() == 2
            assert await kb.ensure_documents() == 0

            listing = await kb.list_documents()
            assert listing["total_documents"] == listing["legacy_documents"] == 2
            assert listing["total_chunks"] == len(rows)
            assert [d["title"] for d in listing["documents"]] == ["long", "warranty"]   # 新的在前
            by_title = {d["title"]: d for d in listing["documents"]}
            assert by_title["warranty"]["display_title"] == "Warranty Review"
            assert by_title["long"]["chunks"] == by_title["long"]["chunk_count"] == len(rows) - 1

            # 稠密和关键词两路都还能召回，命中带文档出处
            hits = await kb.search_async("written limited warranty", top_k=3)
            assert hits[0]["title"] == "warranty" and hits[0]["retrieval"] == "hybrid"
            assert hits[0]["doc_id"] == by_title["warranty"]["id"]
            assert hits[0]["chunk_id"] == rows[0]["id"]

            # 升级后的导入：老文档不重复，新文档正常进
            before = await store.kb_count()
            old, new = await kb.import_documents([
                {"title": "long", "content": LONG_DOC},
                {"title": "new", "content": "LTL freight is scheduled separately."},
            ])
            assert old["status"] == "restored" and new["status"] == "created"
            assert await store.kb_count() == before + 1
            assert (await kb.list_documents(query="LTL freight"))["matched"] == 1
        finally:
            await db.close()
    finally:
        await raw.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await raw.close()


async def test_seeded_library_is_browsable(tmp_path):
    store = LocalStore(db_path=str(tmp_path / "seed.db"))
    try:
        kb = KnowledgeBase(store=store, embedder=Embedder())
        await kb.ensure_seeded()
        listing = await kb.list_documents()
        assert listing["total_documents"] == len(kbmod._DEFAULT_DOCS)
        assert listing["legacy_documents"] == 0
        assert {d["source_type"] for d in listing["documents"]} == {"seed"}
        await kb.ensure_seeded()
        assert (await kb.list_documents())["total_documents"] == len(kbmod._DEFAULT_DOCS)
    finally:
        await store.close()
