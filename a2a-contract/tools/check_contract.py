#!/usr/bin/env python3
"""
契约仓库的自检：schema 自校验 + fixtures 校验 + 文档内链接检查 + 版本号一致性。

四件事都是**机器能做的那部分**（附录 B 自检表第 5 条）。人肉规则会累，
所以凡是形状类的断言都压到这里，不指望下一个人读文档时记得。

    python3 tools/check_contract.py          # 全部
    python3 tools/check_contract.py schema   # 只跑其中一段
    python3 tools/check_contract.py fixtures
    python3 tools/check_contract.py links
    python3 tools/check_contract.py version

依赖：jsonschema（links 段不需要）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "a2a-streaming-contract.md"
SCHEMA_DIR = ROOT / "schema"
FIXTURES = ROOT / "fixtures"
README = ROOT / "README.md"

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  ✗ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


# ── 1. schema 自校验 ─────────────────────────────────────────────────────────

def _validator_cls():
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("需要 jsonschema：pip install jsonschema", file=sys.stderr)
        raise SystemExit(2)
    return Draft202012Validator


def _registry():
    """把本地 schema 按文件名注册，让 $ref: \"database-id.schema.json\" 这类相对引用能解析。"""
    from referencing import Registry, Resource

    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc)
        resources.append((path.name, resource))
        if "$id" in doc:
            resources.append((doc["$id"], resource))
    return Registry().with_resources(resources)


def check_schemas() -> None:
    print("[1/4] schema 自校验")
    cls = _validator_cls()
    files = sorted(SCHEMA_DIR.glob("*.schema.json"))
    if not files:
        fail("schema/ 下一个 *.schema.json 都没有")
        return
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"{path.name}: JSON 解析失败 —— {exc}")
            continue
        try:
            cls.check_schema(doc)
        except Exception as exc:
            fail(f"{path.name}: 不是合法的 JSON Schema —— {exc}")
            continue
        if "$id" not in doc:
            fail(f"{path.name}: 缺 $id")
        if "title" not in doc:
            fail(f"{path.name}: 缺 title")
        ok(f"{path.name}")

    # 契约里点名的枚举必须真的在 schema 里，而不是只写在正文表格里
    stage = json.loads((SCHEMA_DIR / "stage.schema.json").read_text(encoding="utf-8"))
    declared = set(stage["$defs"]["contract-declared"]["enum"])
    was = set(stage["$defs"]["emitted-2026-09-04"]["enum"])
    now = set(stage["$defs"]["emitted-2026-09-08"]["enum"])
    # v1.10：§4.5 的 normalization 缺口关闭。守两条——历史组仍是真子集（它是缺口
    # 存在过的证据，不许被"修"成九个），当前组必须等于 contract-declared。
    if not was < declared:
        fail("stage.schema.json: emitted-2026-09-04 是历史对照，必须仍是真子集")
    elif now != declared:
        fail(f"stage.schema.json: emitted-2026-09-08 必须等于 contract-declared，"
             f"差集 {sorted(declared ^ now)}")
    else:
        ok(f"stage 枚举：声明 {len(declared)} 个、当前实发 {len(now)} 个，"
           f"**相等 ⇒ §4.5 缺口已关闭**（历史对照组仍记着当初缺的 {sorted(declared - was)}）")

    # v1.12：§4.4 的关闭判据——`execution_status` 由可选提级为 required。
    # 判据当初特意挪到契约仓库这一侧（附录 A 的 v1.9 行），图的就是不依赖任何一侧的
    # 测试标记、也不会因为谁 rebase 而失效。**改这一行的那次提交就是关闭证据。**
    dp = json.loads(
        (SCHEMA_DIR / "artifact-data-part.schema.json").read_text(encoding="utf-8")
    )
    if "execution_status" not in dp["properties"]:
        fail("artifact-data-part.schema.json: execution_status 连 properties 都没有")
    elif "execution_status" not in dp["required"]:
        fail("artifact-data-part.schema.json: execution_status 不在 required 里——"
             "§4.4 的关闭判据就是它提级为必填，没提级就不算关闭")
    else:
        ok("execution_status ∈ required ⇒ **§4.4 缺口已关闭**（v1.12 提级，走的 §6）")

    # v1.13：§4.4 的传输终态由两种变成三种。新增的 canceled 必须真的在 schema 里，
    # 而且拼写跟 A2A 规范走——metadata.error 里写的 `cancelled:` 是取消原因，不是取值。
    te = json.loads(
        (SCHEMA_DIR / "terminal-event.schema.json").read_text(encoding="utf-8")
    )
    states = set(te["$defs"]["terminalState"]["enum"])
    if states != {"completed", "failed", "canceled"}:
        fail(f"terminal-event.schema.json: 传输终态应为 completed / failed / canceled，"
             f"实为 {sorted(states)}")
    else:
        ok("传输终态 = completed / failed / canceled ⇒ **§4.4 的 canceled 已进 schema**（v1.13）")

    db = json.loads((SCHEMA_DIR / "database-id.schema.json").read_text(encoding="utf-8"))
    current = set(db["$defs"]["current-2026-09-04"]["enum"])
    retired = set(db["$defs"]["retired-v1.6"]["enum"])
    known = set(db["$defs"]["known"]["enum"])
    # v1.6 起 database 的契约面是 pattern，不是枚举；$defs 只是断言用的具名集合。
    # 这里守两条：退役集与生效集不得相交（否则就是改名没改干净），known 必须是二者的并。
    if current & retired:
        fail(f"database-id.schema.json: current 与 retired 相交 {sorted(current & retired)}")
    elif known != current | retired:
        fail("database-id.schema.json: known 必须等于 current ∪ retired")
    else:
        ok(f"database 契约面 = pattern {db['pattern']}；当前生效 {sorted(current)}、"
           f"v1.6 退役 {sorted(retired)}（mes_db→prod_db 改名、pm_db 并入 erp_db）")
    pattern = re.compile(db["pattern"])
    for ident in sorted(known):
        if not pattern.match(ident):
            fail(f"database-id.schema.json: 已知标识符 {ident} 不匹配自己的 pattern")


# ── 2. fixtures 校验 ─────────────────────────────────────────────────────────

def check_fixtures() -> None:
    print("[2/4] fixtures 校验")
    cls = _validator_cls()
    registry = _registry()
    manifest_path = FIXTURES / "index.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    listed = {c["file"] for c in manifest["cases"]}
    on_disk = {
        str(p.relative_to(FIXTURES))
        for p in FIXTURES.rglob("*.json")
        if p.name != "index.json"
    }
    for orphan in sorted(on_disk - listed):
        fail(f"fixture 文件没进清单：{orphan}（清单分叉就是副本漂移换个地方复发）")
    for missing in sorted(listed - on_disk):
        fail(f"清单里的文件不存在：{missing}")

    seen_ids: set[str] = set()
    for case in manifest["cases"]:
        cid = case["id"]
        if cid in seen_ids:
            fail(f"用例 id 重复：{cid}")
        seen_ids.add(cid)
        for key in ("group", "file", "schema", "expect", "trust", "why"):
            if key not in case:
                fail(f"{cid}: 清单缺字段 {key}")
        path = FIXTURES / case["file"]
        schema_path = ROOT / case["schema"]
        if not path.exists() or not schema_path.exists():
            continue

        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = cls(schema, registry=registry)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        actually_valid = not errors

        want_valid = case["expect"] == "valid"
        if actually_valid != want_valid:
            if want_valid:
                fail(f"{cid}: 期望通过校验，实际失败 —— {errors[0].message}")
            else:
                fail(f"{cid}: 期望校验失败，实际通过了。"
                     f"反例不报错 = 这条断言其实没在保护任何东西")
        else:
            mark = "校验通过" if actually_valid else f"如期被拒（{errors[0].message[:60]}）"
            ok(f"{cid} [{case['group']}] {mark}")

    # v1.12 提级 execution_status 带来的坑：多一条必填 = 每个反例都多一个被拒理由，
    # 于是「这条反例真的在保护它声称保护的东西」就悄悄失效了。除 dp-trust-01
    # （它的职责本来就是「缺这个字段会被挡住」）以外，任何反例都不许因为它被拒。
    for case in manifest["cases"]:
        if case["expect"] != "invalid" or case["id"] == "dp-trust-01":
            continue
        path = FIXTURES / case["file"]
        schema_path = ROOT / case["schema"]
        if not path.exists() or not schema_path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        reasons = [
            e.message for e in cls(schema, registry=registry).iter_errors(payload)
        ]
        stray = [m for m in reasons if "execution_status" in m]
        if stray:
            fail(f"{case['id']}: 被拒的理由里混进了 execution_status —— {stray[0]}。"
                 f"提级不该给既有反例添理由，给它补上这个字段")
    ok("反例的拒绝理由未被 execution_status 提级污染（dp-trust-01 除外，那正是它的职责）")

    groups = {c["group"] for c in manifest["cases"]}
    # F 组 v1.13 起有了 fixture（终态信封）；它的顺序类断言仍要一整条流，见 CONSUMER-TESTS.md
    for required in ("A", "B", "C", "D", "E", "F"):
        if required not in groups:
            fail(f"CONSUMER-TESTS.md 定义了 {required} 组，清单里一个用例都没有")


# ── 3. 文档内链接检查 ────────────────────────────────────────────────────────

_SECTION_RE = re.compile(r"^#{2,4}\s+(?:附录\s*)?([0-9]+(?:\.[0-9]+)*|[A-Z])\.?\s")
# 约定：`§X` 一律指本文的小节。引用外部规范（A2A 官方文档等）写成「规范 N 节」，
# 不带 §——否则这里会把它当成本文的小节去核对，误报，而且读者也分不清在引谁。
_REF_RE = re.compile(r"§\s*([0-9]+(?:\.[0-9]+)*)")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_BACKTICK_PATH_RE = re.compile(r"`((?:schema|fixtures|tools)/[A-Za-z0-9_./-]+)`")


def _headings(text: str) -> set[str]:
    found = set()
    for line in text.split("\n"):
        m = _SECTION_RE.match(line)
        if m:
            found.add(m.group(1))
    return found


def check_links() -> None:
    print("[3/4] 文档内链接检查")
    docs = sorted(p for p in ROOT.rglob("*.md") if ".git" not in p.parts)
    contract_text = CONTRACT.read_text(encoding="utf-8")
    headings = _headings(contract_text)
    # §4.6.1 这类三级引用挂在 #### 标题上，标题行写的是 "4.6.1"，已被上面收进来了
    ok(f"契约小节标题 {len(headings)} 个")

    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for ref in sorted(set(_REF_RE.findall(text))):
            # §9 这类只到一级的引用，命中任何以它开头的小节即可
            if ref in headings:
                continue
            if any(h == ref or h.startswith(ref + ".") for h in headings):
                continue
            fail(f"{doc.relative_to(ROOT)}: §{ref} 指向一个不存在的小节")
        for target in sorted(set(_LINK_RE.findall(text))):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            # 相对链接按**该文件自己所在目录**解析，不是按仓库根
            if not (doc.parent / target.split("#")[0]).exists():
                fail(f"{doc.relative_to(ROOT)}: 相对链接指向不存在的文件 —— {target}")
        for target in sorted(set(_BACKTICK_PATH_RE.findall(text))):
            if not (ROOT / target).exists():
                fail(f"{doc.relative_to(ROOT)}: 正文里点名的文件不存在 —— {target}")
        ok(f"{doc.relative_to(ROOT)}")

# ── 4. 版本号一致性 ──────────────────────────────────────────────────────────

_VER_RE = re.compile(r"v\d+\.\d+")
_CHANGELOG_ROW_RE = re.compile(r"^\|\s*(v\d+\.\d+)\s*\|")     # 契约附录 A 的变更记录表
_README_ROW_RE = re.compile(r"^\|\s*`(v\d+\.\d+)`\s*\|")      # README 的版本表
_PIN_RE = re.compile(r"--branch\s+(v\d+\.\d+)")               # 「钉在这个 tag 上」的示例
_COMPANION_RE = re.compile(r"配套：[^\n]*?（(v\d+\.\d+)）")     # 「配套：…（vX.Y）」


def _vkey(v: str) -> tuple[int, ...]:
    """v1.11 > v1.9。按段比数值，不能按字符串比——字符串比会判 v1.9 更大。"""
    return tuple(int(part) for part in v.lstrip("v").split("."))


def _table_versions(text: str, row_re: re.Pattern) -> list[str]:
    return [m.group(1) for m in (row_re.match(ln) for ln in text.splitlines()) if m]


def _version_docs() -> list[Path]:
    """所有可能写着「当前钉哪一版」的文件：文档 + workflow。"""
    out: list[Path] = []
    for pattern in ("*.md", "*.yml", "*.yaml"):
        out += [p for p in ROOT.rglob(pattern) if ".git" not in p.parts]
    return sorted(out)


def _live_lines(path: Path, text: str) -> list[str]:
    """
    只留下**会被照着执行的行**，丢掉叙述历史的行。

    因为这个仓库的文档大量在谈论旧版本——附录 A 的每一条变更记录、附录 B 的
    漂移对照表、本脚本自己的注释，都会原样写出 `--branch v1.9` 这样的字符串。
    照字面扫全文，写一条「v1.12 把 --branch v1.11 改成 v1.12」的变更记录就会
    把自检搞红，而它明明是对的。**给下一个人留下这种陷阱，比不检查更糟。**

    判据是「这一行会不会被复制粘贴去执行」：
      - Markdown：只认 ``` 围栏里的行（可复制的命令都在围栏里，叙述在围栏外）；
      - YAML：只认非 `#` 注释行（注释是写给人看的，run: 是给机器跑的）。
    """
    lines = text.splitlines()
    if path.suffix in (".yml", ".yaml"):
        return [ln for ln in lines if not ln.lstrip().startswith("#")]
    live, inside = [], False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            live.append(ln)
    return live


def _tag_exists(tag: str) -> bool | None:
    """本地有没有这个 tag。取不到 git 信息就返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "tag", "--list", tag],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(proc.stdout.strip()) if proc.returncode == 0 else None


def check_versions() -> None:
    """
    每一处「当前是哪一版」的声明，都必须等于状态行的版本号。

    **这一段补过两次，两次的教训不一样。**

    第一次（v1.10）补的是**一处**：状态行改了、`fixtures/index.json` 的
    `contract_version` 没跟着改，停在 v1.9。发现它的不是本脚本，是**下游仓库的**
    `test_contract_version_is_the_one_we_pinned`——它按 tag 拉了 v1.10 的目录，
    读出来的版本号却是 v1.9。下游按这个字段判断「我拉到的是不是我钉的那一版」，
    它漂了，下游要么误判，要么报一条看起来像是自己配错了的失败。

    第二次（v1.11）补的是**范围**：`CONSUMER-TESTS.md` 开头那句
    「配套：`a2a-streaming-contract.md`（v1.5）」**活过了 v1.6–v1.10 五个版本**，
    同一份文件末尾的 `git clone --branch v1.9` 也是。而且 ai-helpdesk 早在 v1.6
    就把前一条写进了交回契约的反馈清单——**写了，也没人改**。
    原因不是谁不认真：**没有任何机器在看那两行**，而人肉规则会累。

    所以这里不再逐个点名文件，改成按**形态**收全：凡是长得像「当前钉哪一版」的
    地方——版本表最新行、`contract_version`、`--branch`、「配套：…」——一律与
    状态行比。往后新加的文档只要沿用同样的写法，就自动被这条检查覆盖。
    这正是附录 B 自检表第 5 条要求的：机器能校验的部分交给机器。
    """
    print("[4/4] 版本号一致性")
    contract = CONTRACT.read_text(encoding="utf-8")

    status_line = next(
        (ln for ln in contract.splitlines() if ln.startswith("> 状态：")), None
    )
    if status_line is None:
        fail("契约里找不到状态行（`> 状态：`）——它是附录 B 判别权威副本的第一道判据")
        return
    m = _VER_RE.search(status_line)
    if not m:
        fail(f"状态行里没有 vX.Y 版本号：{status_line[:60]}")
        return
    truth = m.group(0)
    ok(f"状态行 = {truth}（下面全部与它比）")

    # ① 两张版本表的最新行
    for label, text, row_re in (
        ("契约附录 A 变更记录", contract, _CHANGELOG_ROW_RE),
        ("README 版本表", README.read_text(encoding="utf-8"), _README_ROW_RE),
    ):
        rows = _table_versions(text, row_re)
        if not rows:
            fail(f"{label}：一行版本号都没解析到——表格写法变了，这条检查已经悄悄失效")
            continue
        latest = max(rows, key=_vkey)
        if latest != truth:
            fail(f"{label}的最新行是 {latest}，状态行是 {truth}")
        else:
            ok(f"{label}最新行 = {latest}（共 {len(rows)} 行）")

    # ② 下游真正读的那个字段
    index_version = json.loads(
        (FIXTURES / "index.json").read_text(encoding="utf-8")
    )["contract_version"]
    if index_version != truth:
        fail(f"fixtures/index.json 的 contract_version 是 {index_version}，"
             f"状态行是 {truth}——下游按这个字段判断拉到的是不是自己钉的那一版，"
             f"漂了会红在下游，而不是红在这里")
    else:
        ok(f"fixtures/index.json contract_version = {index_version}")

    # ③ 所有「让人钉哪一版」的写法
    checked = 0
    for doc in _version_docs():
        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(ROOT)
        # 「钉在这个 tag 上」的命令：只看会被照着敲的那些行
        for pinned in sorted(set(_PIN_RE.findall("\n".join(_live_lines(doc, text))))):
            checked += 1
            if pinned != truth:
                fail(f"{rel}: 示例让人钉 --branch {pinned}，当前是 {truth}")
        # 「配套：…（vX.Y）」是文档抬头的写法，只在 Markdown 的引用行上成立
        if doc.suffix == ".md":
            for ln in text.splitlines():
                if not ln.lstrip().startswith("> 配套："):
                    continue
                for companion in _COMPANION_RE.findall(ln):
                    checked += 1
                    if companion != truth:
                        fail(f"{rel}: 抬头写着「配套：…（{companion}）」，当前是 {truth}")
    if checked:
        ok(f"钉版本的示例与「配套：」声明共 {checked} 处")
    else:
        fail("一处 --branch / 「配套：」都没扫到——写法变了，这条检查已经失效")

    # ④ tag 存在与否**不作为失败判据**：发布提交总是先于打 tag，在发布分支上跑
    #    自检时它本来就还不存在。但值得打印一行——v1.11 这一版的起因正是
    #    「三处文档都在让下游钉 v1.11，而那个 tag 一直没有被创建」。
    exists = _tag_exists(truth)
    if exists is None:
        print(f"  · tag {truth}：取不到 git 信息，跳过（不影响判定）")
    elif exists:
        ok(f"tag {truth} 本地已存在")
    else:
        print(f"  · tag {truth} 还没打，而文档已经在让下游钉它了。"
              f"合入 main 后补 `git tag {truth}` 并推送（不作为失败判据）")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    known = ("all", "schema", "fixtures", "links", "version")
    if which not in known:
        print(f"未知的段名 {which!r}，可选：{', '.join(known)}", file=sys.stderr)
        return 2
    if which in ("all", "schema"):
        check_schemas()
    if which in ("all", "fixtures"):
        check_fixtures()
    if which in ("all", "links"):
        check_links()
    if which in ("all", "version"):
        check_versions()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
