from __future__ import annotations

from typing import Optional


class SqlAgentError(RuntimeError):
    """AI SQL Agent 统一异常基类。"""


class ModelCallError(SqlAgentError):
    """
    模型调用失败的统一异常。

    把散落在各 Client 里的 `RuntimeError(f"调用失败: {exc}")` 收敛成有类型、
    可判断是否可重试的错误，便于上层按类别处理（限流退避 / 直接失败 / 告警）。
    """

    #: 该类错误是否值得重试
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        component: str = "",
        model: str = "",
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        block_scope: str = "",
        block_reason: str = "",
        block_permanent: bool = False,
        upstream_message: str = "",
    ):
        super().__init__(message)
        self.upstream_message = upstream_message
        """服务端错误响应里那句给人看的话（`error.message`），凭证已去掉；认不出来就是空。"""
        self.component = component
        self.model = model
        self.status_code = status_code
        self.retry_after = retry_after
        self.block_scope = block_scope
        """
        这个错误说明哪个范围不能再用：`credential`（这把凭证）、`model`（这把凭证下的
        这个模型）或空（只是这一次调用失败）。由统一调用入口按响应语义填，
        见 `ai_sql_agent.core.model_access`。
        """
        self.block_reason = block_reason
        self.block_permanent = block_permanent
        """True：重试、换个阶段再调都没有意义，直到凭证修好；False：过一会儿可能就好。"""

    @property
    def access_unavailable(self) -> bool:
        """
        这次失败是不是「凭证 / 额度 / 权限已知不可用」——不是服务抖动。

        上层据此区分两种降级：服务抖动过一会儿会好；这一种在有人修配置之前不会好，
        批量任务（基准、压测）应当立刻停下，而不是逐条撞过去。
        """
        return bool(self.block_scope) and self.block_permanent

    @property
    def category(self) -> str:
        return type(self).__name__

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.component:
            parts.append(f"component={self.component}")
        if self.model:
            parts.append(f"model={self.model}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " | ".join(parts)


class ModelAuthError(ModelCallError):
    """
    鉴权失败（401/403），重试无意义。

    401 与「明确说凭证无效 / 停用」的 403 指向凭证本身（`block_scope="credential"`）；
    「无权使用这个模型」的 403 只指向这个模型（`block_scope="model"`）；
    语义不明的 403 不当成永久（`block_permanent=False`）。
    """

    retryable = False


class ModelQuotaExhaustedError(ModelCallError):
    """
    额度 / 余额明确用尽（402，或响应体里写明了额度耗尽）。

    与普通限流（`ModelRateLimitError`）不是一回事：限流等一等就好，这个不充值不会好，
    重试只是多发几次注定失败的请求。
    """

    retryable = False


class ModelCredentialUnavailableError(ModelAuthError):
    """
    这组凭证（或这把凭证下的这个模型）已知不可用，请求在本地被直接短路，**没有发出去**。

    继承 `ModelAuthError`：对上层来说它就是一次鉴权类失败，走同一条降级链路。
    """

    retryable = False


class ModelBadRequestError(ModelCallError):
    """请求本身不合法（4xx，不含 429）：参数、模型名或内容问题，重试无意义。"""

    retryable = False


class ModelRateLimitError(ModelCallError):
    """触发限流（429）：需要退避后重试。"""

    retryable = True


class ModelServerError(ModelCallError):
    """服务端错误（5xx）：可退避重试。"""

    retryable = True


class ModelTimeoutError(ModelCallError):
    """请求超时：可退避重试。"""

    retryable = True


class ModelConnectionError(ModelCallError):
    """网络连接失败：可退避重试。"""

    retryable = True


class ModelCircuitOpenError(ModelCallError):
    """
    熔断器断开，请求被直接短路。

    重试没有意义（本地就拒绝了），上层应据此走降级链路。
    """

    retryable = False


class ModelResponseError(ModelCallError):
    """返回体结构不符合预期（解析失败）：重试通常无意义。"""

    retryable = False
