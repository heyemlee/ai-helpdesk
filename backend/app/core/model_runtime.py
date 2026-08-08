"""
运行中的模型连接：现在用的是哪一份、怎么换、员工和客户两个实例怎么保持一致。

配置从哪来（core/model_settings.py）：控制台保存的那一份压过 `.env`，没有就用 `.env`。

怎么换：

- **对话**：造一个新客户端，交给每个持有客户端的组件——编排器（连同它的 Agent 和意图识别）、
  工具管理（改写、重排）、记忆（摘要、画像）、评测（判分），各有一个 `reconfigure_llm`。
  正在处理的请求手里拿的是旧客户端，照样跑完。
- **向量**：新旧模型的向量不在一个空间里，不能只改配置。切换任务先用新模型把知识库分片和
  情景记忆的向量全部重算，在存储层一个事务里整批换上，随后让共享的 Embedder 原地换过去，
  最后写配置文件（写不进去就记着，文件可写后自动补写）。重算期间检索照常用旧模型对旧向量；文档导入暂停——重算到一半进来的分片没有
  新向量，那次切换就换不上。

两个实例怎么一致：每个实例每隔几秒看一眼配置文件。员工实例保存之后，客户实例下一次看的时候
跟上：对话直接换；向量要等库里记的向量来源已经是新模型（storage_meta），不然就是拿新模型的
查询向量去比旧库。这几秒里客户实例的检索如果撞上维度不符，会退成只走关键词
（KnowledgeBase.search_async）。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from app.core import model_settings as ms
from app.core.embedder import Embedder
from app.core.llm_utils import create_llm_client, resolve_llm_config
from app.storage.vector_store import EMBEDDING_SIGNATURE_KEY, VectorsChangedError
from app.tools.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

# 每隔多久看一眼配置文件。客户实例最多晚这么久跟上员工实例保存的那份。
WATCH_INTERVAL_S = float(os.getenv("AI_HELPDESK_MODEL_SETTINGS_WATCH_S", "5"))
# 重算向量时一次交给 Embedder 多少段（它内部再按 EMBEDDING_BATCH_SIZE 拆请求）。
# 分段只是为了报进度。
REEMBED_STEP = 64
# 算到一半库里又多了行（新的情景记忆）时，整个重来最多几次。
REEMBED_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


# ── 配置从哪来 ────────────────────────────────────────────────────────────────

def chat_config_from(store: dict[str, Any]) -> dict[str, Any]:
    """
    对话模型连接：页面保存的那一组，没有就用 `.env`。

    返回 `{provider, api_key, model, base_url, source, saved_at, saved_by}`。`.env` 也没配好时
    抛 RuntimeError（和启动时一样）——这时服务本来就起不来。
    """
    page = store.get("chat")
    if page:
        values = page["values"]
        return {
            "provider": values["provider"],
            "api_key": values["api_key"],
            "model": values["model"],
            "base_url": values["base_url"],
            "source": "page",
            "saved_at": page["saved_at"],
            "saved_by": page["saved_by"],
        }
    cfg = resolve_llm_config()
    return {**cfg, "base_url": cfg.get("base_url") or "", "source": "env", "saved_at": "", "saved_by": ""}


def embedder_for(values: dict[str, Any]) -> Embedder:
    """页面上的一组向量连接 → Embedder。一次请求带几段文本仍按 EMBEDDING_BATCH_SIZE。"""
    batch_raw = os.getenv("EMBEDDING_BATCH_SIZE", "").strip()
    return Embedder(
        base_url=values["base_url"],
        api_key=values["api_key"],
        model=values["model"],
        dim=values["dim"],
        batch_size=int(batch_raw) if batch_raw else 10,
        query_prefix=values.get("query_prefix", ""),
    )


def embedder_from(store: dict[str, Any]) -> tuple[Embedder, dict[str, str]]:
    """向量模型连接：页面保存的那一组，没有就用 `.env`。返回 (Embedder, 来源说明)。"""
    page = store.get("embedding")
    if page:
        meta = {"source": "page", "saved_at": page["saved_at"], "saved_by": page["saved_by"]}
        return embedder_for(page["values"]), meta
    return Embedder.from_env(), {"source": "env", "saved_at": "", "saved_by": ""}


def chat_identity(cfg: dict[str, Any]) -> tuple[str, str, str, str]:
    """判断两份对话连接是不是同一份。Key 只比摘要，不留在内存里多一份明文。"""
    digest = hashlib.sha256((cfg.get("api_key") or "").encode("utf-8")).hexdigest()
    return (cfg.get("provider", ""), ms.origin_of(cfg.get("base_url") or ""), cfg.get("model", ""), digest)


# ── 向量切换任务 ──────────────────────────────────────────────────────────────

@dataclass
class SwitchJob:
    """一次向量模型切换的进度。给控制台看，不含 Key。"""

    state: str = "idle"          # idle / running / succeeded / failed
    target_model: str = ""
    target_dim: int = 0
    restoring: bool = False      # True：恢复为服务端配置
    total: int = 0
    done: int = 0
    error: str = ""
    committed: bool = False      # True：库里的向量已经换成新模型（之后再出错也不能说"没有改动"）
    started_at: str = ""
    finished_at: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class ModelRuntime:
    """持有"现在用的是哪一份"，负责换和跟。由 api/main.py 在装配完组件后建一个。"""

    def __init__(
        self,
        *,
        path: str,
        editable: bool,
        chat: dict[str, Any],
        embedder: Embedder,
        embedding_meta: dict[str, str],
        store: Any,
        llm_components: list[Any],
        llm_info: dict[str, str],
    ):
        self.path = path
        self.editable = editable
        self.chat = chat
        self.embedder = embedder
        self.embedding_meta = embedding_meta
        self.job = SwitchJob()
        self._store = store
        self._components = llm_components
        self._llm_info = llm_info
        self._stamp = ms.file_stamp(path)
        self._task: asyncio.Task | None = None
        # 向量已经换上、配置文件却没写进去时，等文件可写了补写的那一组：(values 或 None, saved_by)
        self._unsaved: tuple[dict[str, Any] | None, str] | None = None

    # ── 对话 ──────────────────────────────────────────────────────────────────

    def apply_chat(self, cfg: dict[str, Any]) -> None:
        """换用另一份对话连接：一个新客户端，交给每个组件。"""
        client = create_llm_client(cfg["api_key"], cfg.get("base_url") or None, provider=cfg["provider"])
        for component in self._components:
            component.reconfigure_llm(client, cfg["model"])
        self._llm_info.update(provider=cfg["provider"], model=cfg["model"])
        self.chat = cfg

    # ── 向量 ──────────────────────────────────────────────────────────────────

    @property
    def switching(self) -> bool:
        return self.job.state == "running"

    def start_embedding_switch(
        self, target: Embedder, *, values: dict[str, Any] | None, saved_by: str = ""
    ) -> SwitchJob:
        """
        开始切换到 target。values 是要写进配置文件的那一组；None 表示恢复为服务端配置
        （切换完成后删掉文件里的那一组）。同一时间只能有一个切换在跑。
        """
        if self.switching:
            raise ms.ModelSettingsError("switch_in_progress", "上一次向量模型切换还没结束。")
        self.job = SwitchJob(
            state="running",
            target_model=target.signature,
            target_dim=target.dim,
            restoring=values is None,
            started_at=_now(),
        )
        self._task = asyncio.create_task(self._switch(target, values, saved_by))
        return self.job

    async def _embed_all(self, target: Embedder, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), REEMBED_STEP):
            vectors.extend(await target.embed_many(texts[start:start + REEMBED_STEP]))
            self.job.done += min(REEMBED_STEP, len(texts) - start)
        return vectors

    async def _switch(self, target: Embedder, values: dict[str, Any] | None, saved_by: str) -> None:
        job = self.job
        try:
            for attempt in range(1, REEMBED_ATTEMPTS + 1):
                kb_rows = await self._store.kb_embedding_rows()
                episodic_rows = await self._store.episodic_embedding_rows()
                job.total, job.done = len(kb_rows) + len(episodic_rows), 0
                kb_vectors = await self._embed_all(
                    target, [KnowledgeBase.embedding_text(r["title"], r["content"]) for r in kb_rows]
                )
                # 情景记忆的向量是按摘要算的，见 MemoryManager._store_episodic
                episodic_vectors = await self._embed_all(target, [r["summary"] for r in episodic_rows])
                # 提交之前先告诉在途的写入：从这里起它们手里的向量可能是旧模型的
                self.embedder.mark_cutover()
                try:
                    await self._store.replace_embeddings(
                        target.dim,
                        {r["id"]: v for r, v in zip(kb_rows, kb_vectors, strict=True)},
                        {r["id"]: v for r, v in zip(episodic_rows, episodic_vectors, strict=True)},
                        target.signature,
                    )
                    break
                except BaseException as ex:
                    self.embedder.abort_cutover()
                    if isinstance(ex, VectorsChangedError) and attempt < REEMBED_ATTEMPTS:
                        logger.info("向量重算期间库里多了行，重读一遍再算")
                        continue
                    raise

            # 库里的向量已经是新模型的了。先让运行中的实例跟库一致——不管配置文件
            # 写不写得进去，查询向量都不能再用旧模型去比新向量。
            job.committed = True
            self.embedder.adopt(target)
            self.embedding_meta = {
                "source": "page" if values is not None else "env",
                "saved_at": _now() if values is not None else "",
                "saved_by": saved_by if values is not None else "",
            }
            try:
                self._write_embedding_section(values, saved_by)
            except Exception as ex:  # noqa: BLE001 —— 磁盘满、挂载权限变了……
                self._unsaved = (values, saved_by)
                job.state, job.finished_at = "failed", _now()
                job.error = (
                    f"向量已经换成新模型并在使用，但配置文件没有写进去：{type(ex).__name__}: {ex}。"
                    "文件恢复可写后会自动补写；补写之前不要重启服务。"
                )[:300]
                logger.error(f"向量已换成 {target.signature}，配置文件写入失败，等可写后补写: {job.error}")
                return
            job.state, job.finished_at = "succeeded", _now()
            logger.info(
                f"model_settings.embedding_switched to={target.signature} rows={job.total} "
                f"source={self.embedding_meta['source']} by={saved_by or '-'}"
            )
        except Exception as ex:  # noqa: BLE001 —— 提交之前的失败：库没换就是没换
            job.state, job.finished_at = "failed", _now()
            job.error = f"{type(ex).__name__}: {ex}"[:300]
            logger.warning(f"向量模型切换失败，库里的向量没有改动: {job.error}")

    def _write_embedding_section(self, values: dict[str, Any] | None, saved_by: str) -> None:
        """把这次切换的结果写进配置文件（客户实例看文件跟）。values 为 None = 删掉那一组。"""
        if values is not None:
            ms.save_section(self.path, "embedding", values, saved_by=saved_by)
        else:
            ms.clear_section(self.path, "embedding")
        self._stamp = ms.file_stamp(self.path)

    def _retry_unsaved(self) -> None:
        """上一次切换提交之后配置文件没写进去：文件可写了就补上，再报成功。"""
        if self._unsaved is None:
            return
        values, saved_by = self._unsaved
        try:
            self._write_embedding_section(values, saved_by)
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"配置文件仍然写不进去，稍后再试: {type(ex).__name__}: {ex}")
            return
        self._unsaved = None
        self.job.state, self.job.error = "succeeded", ""
        logger.info("model_settings.embedding_config_saved_late")

    # ── 跟上配置文件 ──────────────────────────────────────────────────────────

    async def sync(self) -> None:
        """
        配置文件变了就跟上。客户实例靠它跟员工实例保存的那份；员工实例自己保存时
        已经换过，这里看到的就是一样的，什么也不做。
        """
        if not self.path or self.switching:
            return
        self._retry_unsaved()
        stamp = ms.file_stamp(self.path)
        if stamp == self._stamp:
            return
        store = ms.load_store(self.path)
        unreadable = (store.get("problems") or {}).get("file")
        if unreadable:
            # 文件坏了不等于有人要恢复 .env：保持现在用的那一份，等文件修好（修好后时间戳会变）
            logger.warning(f"模型连接配置文件{unreadable}，保持现在用的连接不变，等文件修好")
            self._stamp = stamp
            return
        for section, problem in (store.get("problems") or {}).items():
            logger.warning(f"控制台保存的模型连接（{section}）读不出来，按没有处理: {problem}")

        synced = True
        try:
            cfg = chat_config_from(store)
        except RuntimeError as ex:
            logger.warning(f"对话模型连接不可用，保持现在这份: {ex}")
        else:
            if chat_identity(cfg) != chat_identity(self.chat):
                self.apply_chat(cfg)
                logger.info(f"model_settings.chat_followed provider={cfg['provider']} model={cfg['model']}")
            else:
                self.chat = cfg

        target, meta = embedder_from(store)
        if target.signature != self.embedder.signature:
            recorded = await self._store.meta_get(EMBEDDING_SIGNATURE_KEY)
            if recorded == target.signature:
                self.embedder.adopt(target)
                self.embedding_meta = meta
                logger.info(f"model_settings.embedding_followed to={target.signature}")
            else:
                # 配置已经是新的，库里的向量还不是——另一个实例的切换还没换上。下次再看。
                synced = False
        else:
            if target.connection_identity() != self.embedder.connection_identity():
                # 同一个模型、同一个维度：库里的向量不用动，但 Key、地址或查询前缀换了，
                # 这个实例不跟的话会一直拿作废的 Key 去查，检索悄悄退成只走关键词
                self.embedder.adopt(target)
                logger.info(f"model_settings.embedding_connection_followed model={target.signature}")
            self.embedding_meta = meta
        if synced:
            self._stamp = stamp

    async def watch(self) -> None:
        """后台循环：每隔几秒 sync 一次。出错只记日志，循环不能停。"""
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            try:
                await self.sync()
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"跟进模型连接配置失败: {type(ex).__name__}: {ex}")
