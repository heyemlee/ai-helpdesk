"""
给老文档补分类的脚本（scripts/assign_categories.py）：只认一字不差的标题、只动未分类的文档。

这里只测它怎么定计划；真正发请求的那一步走的是 PATCH /knowledge/documents/{id}，
接口本身在 test_workspace_api.py 里测过。
"""
import importlib.util
import json
import pathlib

from app.tools.knowledge_base import _DEFAULT_DOCS


def _load():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "assign_categories.py"
    spec = importlib.util.spec_from_file_location("scripts_assign_categories", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load()


def test_mapping_reads_categories_and_drops_conflicting_titles():
    mapping, conflicts = script.title_categories([
        [{"title": "交期说明", "category": "生产与交期"}, {"title": "没写分类", "content": "x"}],
        [{"title": " 交期说明 ", "category": "production"},              # 同一个分类的两种写法不算冲突
         {"title": "送货费", "category": "delivery"}],
        [{"title": "送货费", "category": "orders"}],                      # 两份资料说法不一：不归
    ])
    assert mapping == {"交期说明": "production"}
    assert conflicts == {"送货费"}


def test_plan_only_touches_uncategorized_documents_with_an_exact_title():
    mapping = {"交期说明": "production", "保修范围与除外情形": "aftersales"}
    moves, unmatched = script.plan([
        {"id": "a", "title": "交期说明", "category": ""},
        {"id": "b", "title": "保修范围与除外情形", "category": "orders"},   # 有人挪过，不动
        {"id": "c", "title": "交期说明（新版）", "category": ""},            # 差一个字也不算
        {"id": "d", "title": "保修范围与除外情形", "category": ""},
    ], mapping)
    assert moves == [
        {"id": "a", "title": "交期说明", "category": "production"},
        {"id": "d", "title": "保修范围与除外情形", "category": "aftersales"},
    ]
    assert [d["id"] for d in unmatched] == ["c"]


def test_bundled_material_covers_every_bundled_title_without_conflicts():
    """库里那 52 篇（6 篇内置 + 46 篇仿真）的标题，照仓库里的资料都能归上类。"""
    sources = [list(_DEFAULT_DOCS)] + [
        json.loads(path.read_text(encoding="utf-8")) for path in script.DEMO_CORPORA
    ]
    mapping, conflicts = script.title_categories(sources)
    assert conflicts == set()
    titles = {d["title"] for d in _DEFAULT_DOCS} | {
        d["title"] for d in json.loads(script.DEMO_CORPORA[0].read_text(encoding="utf-8"))
    }
    assert len(titles) == 52 and titles <= set(mapping)
