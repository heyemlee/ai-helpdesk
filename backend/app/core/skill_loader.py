"""
ai-helpdesk Skill 加载器。

Skill 是一段可热加载的业务能力说明，用来补充 Agent 的 system prompt。
它适合放置企业话术、处理流程、合规边界、排障 SOP 等需要运营侧快速调整的规则。
"""
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """单个 Skill 的标准化表示，屏蔽 Markdown/JSON 等不同文件格式差异。"""
    name: str
    description: str
    content: str
    path: str
    keywords: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    enabled: bool = True

    def matches(self, message: str, agent_type: str | None = None) -> bool:
        """
        判断当前请求是否应该注入这个 Skill。

        - agents 为空：适用于所有 Agent；否则只匹配指定 Agent。
        - keywords 为空：作为全局 Skill 注入；否则只有命中关键词才注入。
        """
        if not self.enabled:
            return False

        if self.agents and agent_type and agent_type.lower() not in self.agents:
            return False

        if not self.keywords:
            return True

        lowered = (message or "").lower()
        return any(keyword.lower() in lowered for keyword in self.keywords)

    # 单个 Skill 的正文上限。3200 是 SOP 还只有中文时定的；补上中英术语对照之后
    # 订单那份 3251 字，正好越线——而截断是**从尾部切**，切掉的恰恰是术语表。
    # 总预算仍由 SkillManager.max_prompt_chars（默认 5000）兜着，而每份 SOP 都
    # 用 agents 限定了只对自己那个 Agent 生效，实际同时命中的基本只有一份。
    MAX_BODY_CHARS = 4000

    def to_prompt_block(self, max_chars: int | None = None) -> str:
        """格式化为可直接拼入 system prompt 的文本块，并限制单个 Skill 长度。"""
        max_chars = self.MAX_BODY_CHARS if max_chars is None else max_chars
        body = self.content.strip()
        if len(body) > max_chars:
            # 截断必须喊出来。之前是静默的：SOP 被切掉尾巴，Agent 少看到几条规则，
            # 日志里没有任何痕迹，只能等某次回答越界了才回头查。
            logger.warning(
                "Skill %s 正文 %d 字超过单份上限 %d，尾部被截断（丢失 %d 字）",
                self.name, len(body), max_chars, len(body) - max_chars,
            )
            body = body[:max_chars].rstrip() + "\n..."
        description = f"\n说明: {self.description}" if self.description else ""
        return f"### {self.name}{description}\n{body}"

    def to_summary(self) -> dict[str, Any]:
        """返回 API 可序列化摘要，避免把完整长文本默认暴露给健康检查。"""
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "keywords": self.keywords,
            "agents": self.agents,
            "enabled": self.enabled,
            "content_chars": len(self.content),
        }


class SkillManager:
    """
    从目录中发现、解析并管理 Skills。

    支持两种常用结构：
      1. skills/order_logistics/SKILL.md
      2. skills/order_logistics.json / .md / .txt
    """

    SUPPORTED_SUFFIXES = {".md", ".txt", ".json"}

    def __init__(self, root_dir: str, max_prompt_chars: int = 5000):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.max_prompt_chars = max_prompt_chars
        self._skills: list[Skill] = []
        self._errors: list[str] = []

    @property
    def skills(self) -> list[Skill]:
        return list(self._skills)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def load(self) -> list[Skill]:
        """重新扫描目录并加载 Skills；单个文件失败不会影响其他 Skill 生效。"""
        loaded: list[Skill] = []
        errors: list[str] = []

        if not self.root_dir.exists():
            logger.info(f"Skill 目录不存在，跳过加载: {self.root_dir}")
            self._skills = []
            self._errors = []
            return []

        for path in self._discover_files(self.root_dir):
            try:
                skill = self._load_file(path)
                if skill is not None:
                    loaded.append(skill)
            except Exception as ex:
                msg = f"{path}: {ex}"
                errors.append(msg)
                logger.warning(f"Skill 加载失败: {msg}")

        self._skills = loaded
        self._errors = errors
        self._log_loaded_skills()
        return self.skills

    def reload(self) -> list[Skill]:
        """运行时热加载入口，供 API 调用。"""
        return self.load()

    def names_for(self, message: str, agent_type: str | None = None) -> list[str]:
        """这句话对这个 Agent 命中了哪些 Skill（名字）。事件日志的 agent_started 记它，不管预算截断。"""
        return [skill.name for skill in self._skills if skill.matches(message, agent_type)]

    def prompt_for(self, message: str, agent_type: str | None = None) -> str:
        """
        为当前用户请求构建 Skill prompt。

        只注入匹配的 Skill，并按总长度截断，避免挤占主对话上下文。
        """
        blocks: list[str] = []
        matched: list[tuple[Skill, list[str]]] = []
        remaining = self.max_prompt_chars
        lowered_message = (message or "").lower()

        for skill in self._skills:
            if not skill.matches(message, agent_type):
                continue
            matched_keywords = [
                keyword for keyword in skill.keywords
                if keyword.lower() in lowered_message
            ]
            block = skill.to_prompt_block()
            if len(block) > remaining:
                logger.warning(
                    "Skill prompt 总预算 %d 字用尽，%s 被截断（丢失 %d 字）",
                    self.max_prompt_chars, skill.name, len(block) - remaining,
                )
                block = block[:remaining].rstrip() + "\n..."
            blocks.append(block)
            matched.append((skill, matched_keywords))
            remaining -= len(block)
            if remaining <= 0:
                break

        if not blocks:
            logger.debug(
                "Skills 未命中: agent=%s message=%r",
                agent_type or "all",
                (message or "")[:80],
            )
            return ""

        detail = "; ".join(
            f"{skill.name}(keywords={', '.join(keywords) if keywords else 'all'})"
            for skill, keywords in matched
        )
        logger.info(
            "Skills 已注入: agent=%s matched=%s message=%r",
            agent_type or "all",
            detail,
            (message or "")[:80],
        )

        return (
            "以下是当前请求可用的 ai-helpdesk Skills。"
            "请优先遵循这些业务规则；如果与系统角色冲突，以系统角色和安全边界为准。\n\n"
            + "\n\n".join(blocks)
        )

    def summary(self) -> dict[str, Any]:
        """返回 Skill 管理器状态，用于 /skills 接口和排障。"""
        return {
            "root_dir": str(self.root_dir),
            "count": len(self._skills),
            "skills": [skill.to_summary() for skill in self._skills],
            "errors": self.errors,
        }

    def _log_loaded_skills(self) -> None:
        """在控制台输出醒目的 Skill 加载结果，方便启动和热加载时确认生效状态。"""
        lines = [
            "",
            "================ ai-helpdesk Skills Loaded ================",
            f"目录: {self.root_dir}",
            f"数量: {len(self._skills)}",
        ]

        if self._skills:
            for index, skill in enumerate(self._skills, start=1):
                agents = ", ".join(skill.agents) if skill.agents else "all"
                keywords = ", ".join(skill.keywords[:8]) if skill.keywords else "all"
                if len(skill.keywords) > 8:
                    keywords += ", ..."
                lines.extend([
                    f"{index}. {skill.name}",
                    f"   agents: {agents}",
                    f"   keywords: {keywords}",
                    f"   path: {skill.path}",
                ])
        else:
            lines.append("未加载任何 Skill。")

        if self._errors:
            lines.append("解析错误:")
            lines.extend(f"  - {error}" for error in self._errors)

        lines.append("========================================================")
        logger.info("\n".join(lines))

    def _discover_files(self, root_dir: Path) -> Iterable[Path]:
        """发现可加载文件，优先读取目录规范文件 SKILL.md。"""
        skill_md_files = sorted(root_dir.rglob("SKILL.md"))
        yielded = {path.resolve() for path in skill_md_files}
        for path in skill_md_files:
            yield path

        for path in sorted(root_dir.rglob("*")):
            resolved = path.resolve()
            if resolved in yielded or not path.is_file():
                continue
            if path.name.startswith(".") or path.name.upper() == "README.MD":
                continue
            if path.suffix.lower() in self.SUPPORTED_SUFFIXES:
                yield path

    def _load_file(self, path: Path) -> Skill | None:
        if path.suffix.lower() == ".json":
            return self._load_json(path)
        return self._load_text(path)

    def _load_json(self, path: Path) -> Skill | None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("JSON Skill 必须是对象格式")

        content = str(raw.get("content") or raw.get("instructions") or "").strip()
        if not content:
            raise ValueError("缺少 content 或 instructions")

        return Skill(
            name=str(raw.get("name") or path.stem),
            description=str(raw.get("description") or ""),
            content=content,
            path=str(path),
            keywords=self._as_list(raw.get("keywords")),
            agents=[item.lower() for item in self._as_list(raw.get("agents"))],
            enabled=self._as_bool(raw.get("enabled"), default=True),
        )

    def _load_text(self, path: Path) -> Skill | None:
        raw = path.read_text(encoding="utf-8")
        meta, body = self._split_front_matter(raw)
        body = body.strip()
        if not body:
            return None

        default_name = path.parent.name if path.name == "SKILL.md" else path.stem
        name = str(meta.get("name") or self._first_heading(body) or default_name)

        # 如果首行标题只是 Skill 名称，注入 prompt 时去掉它，减少重复噪音。
        body = self._strip_first_heading(body, name)

        return Skill(
            name=name,
            description=str(meta.get("description") or ""),
            content=body,
            path=str(path),
            keywords=self._as_list(meta.get("keywords")),
            agents=[item.lower() for item in self._as_list(meta.get("agents"))],
            enabled=self._as_bool(meta.get("enabled"), default=True),
        )

    def _split_front_matter(self, raw: str) -> tuple[dict[str, Any], str]:
        """
        解析 Markdown 顶部的简单 front matter。

        这里刻意不用 PyYAML，避免为一个轻量配置格式新增运行时依赖。
        """
        text = raw.lstrip()
        if not text.startswith("---"):
            return {}, raw

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, raw

        meta: dict[str, Any] = {}
        end_idx: int | None = None
        for idx, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = idx
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")

        if end_idx is None:
            return {}, raw
        return meta, "\n".join(lines[end_idx + 1:])

    @staticmethod
    def _first_heading(body: str) -> str | None:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None

    @staticmethod
    def _strip_first_heading(body: str, name: str) -> str:
        lines = body.splitlines()
        if not lines:
            return body
        first = lines[0].strip()
        if first.startswith("#") and first.lstrip("#").strip() == name:
            return "\n".join(lines[1:]).strip()
        return body

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}
