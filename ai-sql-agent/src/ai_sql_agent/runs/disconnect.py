"""
请求处理期间盯着客户端是否断开。

同步 pipeline 跑在线程里，事件循环这边没人知道客户端已经走了。这里每隔一小段时间问一次
`request.is_disconnected()`，断开就调 `on_disconnect`（通常是取消这次 run），然后等线程在
下一个检查点收尾——结果已经没人接收，但审计照写、run 记录照常结束。

是否启用由入口按 `Settings.cancel_on_disconnect` 决定（a2a-contract v1.13 §5.4 写的是"调用方断开后，
我方在下一个检查点停止"，默认开）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


async def watch_disconnect(
    request: Any,
    work: Awaitable[T],
    on_disconnect: Callable[[], None],
    *,
    poll_seconds: float = 0.5,
) -> T:
    """等 `work` 完成；期间客户端断开就调一次 `on_disconnect`，然后照样等它收尾。"""
    task = asyncio.ensure_future(work)
    signalled = False
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=poll_seconds)
            if done:
                return task.result()
            if not signalled and await request.is_disconnected():
                signalled = True
                on_disconnect()
    except asyncio.CancelledError:
        # 处理这个请求的协程被取消（服务关停时）：一样通知，再把取消往上抛
        if not signalled:
            on_disconnect()
        raise
