"""
按积压降级：排队的 run 越多，每一轮做得越省。

**为什么看积压。** 慢的原因可能是模型限流、ai-sql-agent 慢、worker 不够——不管是哪个，
都先表现为队列变长。积压是所有这些原因共同的领先指标，而且 worker 领 run 的时候一次
XINFO 就能读到，不用去猜是哪一环慢了。

档位是累加的，一档比一档省（省下来的量见 docs/evaluation/tool-loop-ab.md 等）：

| 档 | 关掉什么 | 省下什么 |
|---|---|---|
| 1 | 工具循环 | 每轮平均少一半输入 token（A/B 里 4,915 → 2,692）和一次可能的委派 |
| 2 | 检索的改写和重排 | 两次模型往返（走规则通道那条直检） |
| 3 | 委派查库 | 一次 17～40 秒、还要花下游模型的调用；订单类问题只按知识库和 SOP 答 |
| 4 | 这一轮不出网 | 全部模型调用：按"模型服务不可用"答，不建工单（和停机时同一条路） |

**最后一档不转人工。** 计划里写的是"直接转人工"，做的时候改了：过载是几分钟的事，
这时候每条提问开一张工单，工单队列里全是过载的回声，真正需要人的那几张被淹掉——
和 docs/ops/model-outage-handling.md 里停机不开工单是同一个理由。客户看到的是"暂时
无法回答，请稍后再试；要人工请回复转人工"。

| 变量 | 默认 | 说明 |
|---|---|---|
| AI_HELPDESK_DEGRADE_BACKLOG | 20,40,80,160 | 积压到这四个数时分别进第 1～4 档；off 不降级 |
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = (20, 40, 80, 160)

_WHAT = {
    1: "tool_loop",
    2: "rewrite_rerank",
    3: "order_data",
    4: "model_calls",
}


@dataclass(frozen=True)
class Degrade:
    """这一轮按什么档位跑。level 0 是不降级。"""

    level: int = 0
    backlog: int = 0
    threshold: int = 0

    @property
    def no_tool_loop(self) -> bool:
        return self.level >= 1

    @property
    def direct_search(self) -> bool:
        return self.level >= 2

    @property
    def no_delegation(self) -> bool:
        return self.level >= 3

    @property
    def shed(self) -> bool:
        return self.level >= 4

    def to_dict(self) -> dict:
        """进事件日志和响应：哪一档、当时积压多少、关掉了哪些。"""
        return {
            "level": self.level,
            "backlog": self.backlog,
            "threshold": self.threshold,
            "off": [_WHAT[n] for n in range(1, self.level + 1)],
        }


NONE = Degrade()


@dataclass(frozen=True)
class DegradePolicy:
    thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS

    @property
    def enabled(self) -> bool:
        return bool(self.thresholds)

    def level_for(self, backlog: int) -> Degrade:
        level, threshold = 0, 0
        for n, at in enumerate(self.thresholds, start=1):
            if backlog >= at:
                level, threshold = n, at
        return Degrade(level=level, backlog=backlog, threshold=threshold) if level else NONE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DegradePolicy:
        env = os.environ if env is None else env
        raw = env.get("AI_HELPDESK_DEGRADE_BACKLOG", "").strip().lower()
        if not raw:
            return cls()
        if raw in ("off", "0", "false", "no"):
            return cls(thresholds=())
        try:
            values = tuple(int(part) for part in raw.split(","))
        except ValueError:
            values = ()
        if len(values) != 4 or any(v <= 0 for v in values) or list(values) != sorted(values):
            logger.warning("AI_HELPDESK_DEGRADE_BACKLOG=%r 要四个递增的正整数，按默认 %s",
                           raw, ",".join(map(str, DEFAULT_THRESHOLDS)))
            return cls()
        return cls(thresholds=values)
