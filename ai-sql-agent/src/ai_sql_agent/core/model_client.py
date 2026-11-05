"""
主链路的三个模型 Client（规划、SQL 生成、抽词）共用的那一段：
连接参数从哪儿取，以及页面换了模型连接之后怎么跟上。
"""
from __future__ import annotations

import threading
from typing import Any, NamedTuple

from .model_settings import SettingsBinding
from .settings import get_settings
from .transport import ModelTransport


class ModelConnection(NamedTuple):
    """
    一次调用用的连接。

    这几样是在同一把锁里一起取出来的，来自同一份配置：取出来之后配置再换，
    这一次调用也不会出现「新地址配旧 Key」。
    """

    transport: ModelTransport
    url: str
    model: str
    has_key: bool


class FollowsModelSettings:
    """
    给模型 Client 用的 mixin：构造时把没写的连接参数从全局配置补齐，
    之后每次调用前看一眼模型连接有没有换过，换过就把当初取自配置的那几项重新取一遍。

    Client 以前是在构造时把 Key、地址、模型名各抄一份，之后再也不看配置。服务启动时建好的
    那几个 Client 会活到进程结束，所以改模型连接只能重启。现在管理员可以在页面上改
    （见 `ai_sql_agent.core.model_settings`），保存后下一次调用就得用上新的。

    构造时显式传进来的参数（测试、脚本常这么用）不跟着变——那是调用方自己的决定。
    """

    COMPONENT = ""
    URL_FIELD = "base_url"
    """配置对象上放接口地址的字段名（抽词 Client 叫 `chat_url`）。"""
    MODEL_SETTING = "chat_model"
    """模型名取全局配置的哪一项。"""

    def _bind_connection(self, config: Any) -> None:
        self.config = config
        self._binding = SettingsBinding(
            config,
            {
                "api_key": lambda settings: settings.resolved_chat_api_key(),
                self.URL_FIELD: lambda settings: settings.chat_url,
                "model": lambda settings: getattr(settings, self.MODEL_SETTING),
                "provider": lambda settings: settings.model_provider,
            },
        )
        self._connection_lock = threading.Lock()
        self.transport = None
        self._connection()

    def _connection(self) -> ModelConnection:
        with self._connection_lock:
            if self._binding.refresh(get_settings) or self.transport is None:
                # Key 和协议是 transport 的一部分，所以它们一换就整个重建
                self.transport = ModelTransport(
                    self.COMPONENT,
                    api_key=self.config.api_key,
                    timeout=self.config.timeout,
                    provider=self.config.provider,
                )
            return ModelConnection(
                transport=self.transport,
                url=getattr(self.config, self.URL_FIELD),
                model=self.config.model,
                has_key=bool(self.config.api_key),
            )

    def model_available(self) -> bool:
        """按当前的模型连接，现在有没有 Key 可用（会先跟上页面保存的连接）。"""
        return self._connection().has_key
