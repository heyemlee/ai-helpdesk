"""
事件日志的保留期：进程里一个每天跑一次的任务，只处理本实例的行。

- body（原文）到期清空成 NULL，结构留着：员工实例 30 天、客户实例 14 天；
- 整行到期删除：180 天。

两个数都是变量（AI_HELPDESK_EVENT_LOG_BODY_DAYS / _DAYS），配成 0 才不清。这和对话审计
的做法不一样：审计定的原则是"服务不悄悄删数据"。这里默认就清，是因为原文副本放着不管，
就是一份越积越多的个人信息；默认不清的话，忘了配就等于永久保留。清了多少、上次什么时候
跑的，日志和 /health 里都看得到，不算"悄悄"。放在进程里而不是外部定时任务，是因为开机
自启定了不做，机器重启后要有人手动起 compose，外部的定时任务指望不上。
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.events.recorder import EventLogStats

logger = logging.getLogger(__name__)

DAY_S = 86400.0


class Retention:
    def __init__(
        self,
        store,
        *,
        body_days: int,
        row_days: int,
        stats: EventLogStats,
        interval_s: float = DAY_S,
        first_delay_s: float = 60.0,
    ):
        self._store = store
        self.body_days = body_days
        self.row_days = row_days
        self._stats = stats
        self._interval_s = interval_s
        self._first_delay_s = first_delay_s
        self.last_run: float | None = None
        self.last_result: dict[str, int] = {}

    @property
    def active(self) -> bool:
        return self.body_days > 0 or self.row_days > 0

    async def run_once(self, now: float | None = None) -> dict[str, int]:
        """清一次。先删整行，再清剩下那些的原文：删掉的不用再清。"""
        now = time.time() if now is None else now
        rows = (await self._store.delete_rows(now - self.row_days * DAY_S)
                if self.row_days > 0 else 0)
        bodies = (await self._store.purge_bodies(now - self.body_days * DAY_S)
                  if self.body_days > 0 else 0)
        self.last_run = now
        self.last_result = {"bodies_cleared": bodies, "rows_deleted": rows}
        logger.info("事件日志保留期：清空 %d 条原文（超过 %d 天），删除 %d 行（超过 %d 天）",
                    bodies, self.body_days, rows, self.row_days)
        return self.last_result

    async def run_forever(self) -> None:
        """启动后等一会儿先跑一次（别和启动抢连接），之后每天一次。出错只记日志，第二天再来。"""
        await asyncio.sleep(self._first_delay_s)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self._stats.error(ex)
                logger.warning("事件日志保留期清理失败，明天再试: %s", ex)
            await asyncio.sleep(self._interval_s)

    def health(self) -> dict:
        return {"body_days": self.body_days, "row_days": self.row_days,
                "last_run": self.last_run, **self.last_result}
