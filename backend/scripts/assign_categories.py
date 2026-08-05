#!/usr/bin/env python3
"""
给加分类之前导入的文档补上分类。

什么时候用：知识库加了分类之后，以前导入的文档都在"未分类"里。仓库自带的资料
（内置的 6 篇默认文档、data/demo_docs 下的示例和仿真资料）每篇都写着自己的分类，
这个脚本照着它们给库里对应的文档归类，省得在工作台里一篇篇点。

怎么认"对应的文档"：只认导入时的标题和资料里的标题一字不差。不按内容猜、不按关键词猜——
对不上的照旧留在未分类里，由人去归。同一个标题在资料里写了两个不同的分类，也不归，列出来。
**只动未分类的文档**：已经有分类的（可能是有人特意挪过的）一律不碰。

走的是服务的接口（GET /knowledge/documents、PATCH /knowledge/documents/{id}），所以对
本机开发的后端、Docker 里的实例都一样；改分类不碰分片和向量，服务不用停，也不调用任何模型。

用法：
    python scripts/assign_categories.py                                  # 只打印计划
    python scripts/assign_categories.py --apply                          # 照计划归类
    python scripts/assign_categories.py --api http://127.0.0.1:8080 --apply
    python scripts/assign_categories.py --corpus my_docs.json --apply    # 另加一份带分类的资料

实例配了 AI_HELPDESK_API_KEY 的话，用 --api-key 或同名环境变量带上。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from typing import Any

_ROOT = pathlib.Path(__file__).parent.parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.tools.knowledge_base import (  # noqa: E402
    _DEFAULT_DOCS,
    CATEGORIES,
    normalize_category,
)

DEMO_CORPORA = (
    _ROOT / "data" / "demo_docs" / "sample_ca_factory_knowledge.json",
    _ROOT / "data" / "demo_docs" / "sample_knowledge.json",
)


def title_categories(sources: list[list[dict[str, Any]]]) -> tuple[dict[str, str], set[str]]:
    """资料里的 标题 → 分类，以及同一标题写了不同分类的那些标题（不归）。没写分类的条目跳过。"""
    mapping: dict[str, str] = {}
    conflicts: set[str] = set()
    for docs in sources:
        for doc in docs:
            title = str(doc.get("title") or "").strip()
            if not title or not doc.get("category"):
                continue
            key = normalize_category(doc["category"])
            if mapping.get(title, key) != key:
                conflicts.add(title)
            mapping.setdefault(title, key)
    for title in conflicts:
        mapping.pop(title, None)
    return mapping, conflicts


def plan(documents: list[dict[str, Any]], mapping: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(要归类的 [{id, title, category}], 对不上、留在未分类的文档)。已有分类的文档不出现在任何一边。"""
    moves, unmatched = [], []
    for doc in documents:
        if doc.get("category"):
            continue
        key = mapping.get(str(doc.get("title") or "").strip())
        if key:
            moves.append({"id": doc["id"], "title": doc["title"], "category": key})
        else:
            unmatched.append(doc)
    return moves, unmatched


def _call(api: str, path: str, api_key: str, method: str = "GET", body: dict | None = None) -> Any:
    request = urllib.request.Request(
        f"{api.rstrip('/')}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"X-API-Key": api_key} if api_key else {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or "null")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8080", help="服务地址（默认员工实例）")
    parser.add_argument("--api-key", default=os.getenv("AI_HELPDESK_API_KEY", ""), help="员工钥匙")
    parser.add_argument("--corpus", action="append", default=[], help="另加一份带 category 的 JSON 资料")
    parser.add_argument("--apply", action="store_true", help="照计划归类（不加只打印计划）")
    args = parser.parse_args()

    sources = [list(_DEFAULT_DOCS)]
    for path in [*DEMO_CORPORA, *map(pathlib.Path, args.corpus)]:
        sources.append(json.loads(path.read_text(encoding="utf-8")))
    mapping, conflicts = title_categories(sources)

    try:
        listing = _call(args.api, "/knowledge/documents?limit=500", args.api_key)
    except urllib.error.HTTPError as ex:
        print(f"读不到文档列表：{ex.code} {ex.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as ex:
        print(f"连不上 {args.api}：{ex.reason}", file=sys.stderr)
        return 1
    if "categories" not in listing:
        print("这个实例还不支持分类（列表里没有 categories），先部署新版本。", file=sys.stderr)
        return 1
    if listing["matched"] > len(listing["documents"]):
        print(f"注意：库里有 {listing['matched']} 篇，这次只看了前 {len(listing['documents'])} 篇。")

    moves, unmatched = plan(listing["documents"], mapping)
    names = {c["key"]: c["name"] for c in CATEGORIES}
    filed = len(listing["documents"]) - len(moves) - len(unmatched)
    print(f"共 {len(listing['documents'])} 篇：已有分类 {filed} 篇（不动），"
          f"可以照资料归类 {len(moves)} 篇，对不上 {len(unmatched)} 篇（留在未分类）。")
    for key in names:
        titles = [m["title"] for m in moves if m["category"] == key]
        if titles:
            print(f"  {names[key]}（{len(titles)}）：{'、'.join(titles)}")
    for doc in unmatched:
        print(f"  对不上：{doc['title']}（{doc['id']}）")
    for title in sorted(conflicts):
        print(f"  资料里分类不一致，不归：{title}")

    if not args.apply:
        print("只是计划，什么都没有改。确认无误后加 --apply。")
        return 0
    failed = 0
    for move in moves:
        try:
            _call(args.api, f"/knowledge/documents/{move['id']}", args.api_key, "PATCH",
                  {"category": move["category"]})
        except urllib.error.HTTPError as ex:
            failed += 1
            print(f"  没改成：{move['title']}：{ex.code} {ex.read().decode(errors='replace')}", file=sys.stderr)
    print(f"已归类 {len(moves) - failed} 篇" + (f"，{failed} 篇没改成" if failed else "") + "。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
