"""
启动前置校验：让"配错了"变成**起不来**，而不是**悄悄不设防**。

这里的每一条都对应一个"运行时看起来完全正常、只是安全属性没了"的形态。
这类缺陷不会有症状——没有报错、没有告警、日志里也看不出来，只有等到出事
才发现某个开关从来没打开过。所以判定点必须是启动，不是请求。

**为什么不在请求路径上补救。** 曾经的写法是 `if not _API_KEY: return`
（放行）——它把"运维漏配了一个环境变量"翻译成了"这个端点不需要鉴权"。
一个防护措施的缺席被当成了"不需要防护"，和 `core.evidence` 里那个
fail-open 是同一个错误模式。正确的做法是让进程根本起不来：漏配是部署事故，
应该在部署那一刻炸，而不是在第一个攻击者到达时才体现。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# `APP_ENV` 是这个仓库既有的约定（`api/main.py` 用它决定 uvicorn 要不要 reload，
# `.env.example` 里默认就是 production）。这里沿用，不新造一个环境变量——
# 两个含义重叠的开关早晚会配出互相矛盾的组合。
PRODUCTION_ENVS = frozenset({"production", "prod"})

_TRUE = frozenset({"1", "true", "yes", "on"})

# 外层工具超时是在 `SQL_AGENT_TIMEOUT_S` 之上留给连接建立的余量。
# 定义在这里是为了让"注册工具时用的值"和"启动断言检查的值"是同一个算式，
# 两处各写一遍的话，改了一处就等于把断言变成了检查别的东西。
OUTER_TIMEOUT_MARGIN_S = 2.0


class InsecureDeploymentError(RuntimeError):
    """生产模式下安全配置不完整。**不要 catch 它去继续启动。**"""


class ContractViolationError(RuntimeError):
    """启动参数违反 a2a-contract 的硬约束。"""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _flag(env: Mapping[str, str], name: str, default: str = "") -> bool:
    return env.get(name, default).strip().lower() in _TRUE


def is_production(env: Mapping[str, str] | None = None) -> bool:
    return _env(env).get("APP_ENV", "").strip().lower() in PRODUCTION_ENVS


# ── 1. 认证配置完整性 ─────────────────────────────────────────────────────────

def security_config_problems(env: Mapping[str, str] | None = None) -> list[str]:
    """
    列出生产模式下缺的安全配置。返回空列表表示配置完整。

    三项，缺任何一项都不能上线：

    - `AI_HELPDESK_API_KEY` —— 写操作与管理端点的钥匙。**漏配它最严重的
      后果不是管理端点敞开，是 `/auth/token` 敞开**：那个端点的鉴权依赖就是
      它，而请求体里的 `user_id` 由调用方指定。于是"配了 AUTH_SECRET、漏配
      API_KEY"这个组合下，未认证请求可以为**任意用户**签发有效令牌，
      拿着它去读那个用户的历史摘要和画像。两个开关各自看都像是打开的。
    - `AI_HELPDESK_AUTH_SECRET` —— 没有它 `/chat` 的 user_id 就是请求体里
      客户端随便填的那个，跨用户记忆读取只差一次改包。
    - `AI_HELPDESK_PROTECT_CHAT` —— `/chat` 每次都真实调用 LLM，
      公网暴露且不鉴权等于把账单开放给所有人。
    """
    env = _env(env)
    problems: list[str] = []
    if not env.get("AI_HELPDESK_API_KEY", "").strip():
        problems.append(
            "AI_HELPDESK_API_KEY 未配置：写操作/管理端点无鉴权，"
            "且 /auth/token 会为请求体里任意指定的 user_id 签发有效令牌"
        )
    if not env.get("AI_HELPDESK_AUTH_SECRET", "").strip():
        problems.append(
            "AI_HELPDESK_AUTH_SECRET 未配置：/chat 的 user_id 取自请求体，"
            "任何人改一个 id 就能读到别人的记忆上下文"
        )
    if not _flag(env, "AI_HELPDESK_PROTECT_CHAT", "false"):
        problems.append(
            "AI_HELPDESK_PROTECT_CHAT 不是 true：/chat 未鉴权，"
            "每次调用都是真实 LLM 花费"
        )
    return problems


def check_security_config(env: Mapping[str, str] | None = None) -> None:
    """
    生产模式下强制校验认证配置完整性，缺任何一项**启动失败**。

    非生产模式只记一条 info——本地联调要能打开就用，这是 demo 模式的全部意义。
    """
    if not is_production(env):
        return
    problems = security_config_problems(env)
    if not problems:
        return
    raise InsecureDeploymentError(
        "APP_ENV 是生产模式，但安全配置不完整，拒绝启动：\n  - "
        + "\n  - ".join(problems)
        + "\n把上面每一项配齐再启动。确实是在本地联调，就把 APP_ENV 设成 development。"
    )


# ── 2. 超时预算的联动（a2a-contract §1③）─────────────────────────────────────

def outer_tool_timeout_s(env: Mapping[str, str] | None = None) -> float:
    """注册 `order_data_query` 工具时用的超时（= 客户端超时 + 连接建立余量）。"""
    return float(_env(env).get("SQL_AGENT_TIMEOUT_S", "45")) + OUTER_TIMEOUT_MARGIN_S


def a2a_send_budget_s(env: Mapping[str, str] | None = None) -> float:
    """A2A `message/send` 的固定总预算（契约 §1③）。"""
    return float(_env(env).get("SQL_AGENT_A2A_SEND_TIMEOUT_S", "45"))


def check_timeout_budgets(env: Mapping[str, str] | None = None) -> list[str]:
    """
    校验超时档位的联动，返回警告列表（硬约束不满足时直接抛）。

    **硬约束：外层工具超时必须严格大于 send 预算**（契约 v1.6 §1③ 新增）。
    两个值的关系决定超时由谁先判：

    - 外层 > send：send 先超时，抛的是 A2A 的错误，调用方知道是"下游没答完"，
      可以按 §5 回落。今天 47 > 45，顺序是对的。
    - 外层 ≤ send：外层先杀，**send 的预算等于没生效**——改 send 的值不会有
      任何效果，而且不会有任何症状。契约 §1③ 的三档切换正是靠改 send 生效的，
      所以这个关系一破，"切档"这件事本身就静默失效了。

    这一条契约里是硬要求但**没有任何自动检查在防它**（CONSUMER-TESTS
    「这份文件没有覆盖什么」明写了这一点），所以补在这里。
    """
    env = _env(env)
    outer, send = outer_tool_timeout_s(env), a2a_send_budget_s(env)
    if outer <= send:
        raise ContractViolationError(
            f"外层工具超时 {outer}s 不大于 A2A send 预算 {send}s（契约 §1③）。"
            f"这个顺序下外层会先把请求杀掉，send 预算完全不生效，"
            f"改 SQL_AGENT_A2A_SEND_TIMEOUT_S 不会有任何效果。"
            f"抬 SQL_AGENT_TIMEOUT_S（现 {env.get('SQL_AGENT_TIMEOUT_S', '45')}，"
            f"外层 = 它 + {OUTER_TIMEOUT_MARGIN_S}）或压低 send 预算。"
        )

    warnings: list[str] = []
    # 档位判据：契约 §1③ 的第三档（send 90s）要求"流式真的承接流量"。
    # 用户看不到进度就把等待放宽到 90s，正是契约那条硬要求要防的事——
    # 把 25s 空白屏换成 90s 空白屏比现状更糟。
    transport = env.get("SQL_AGENT_TRANSPORT", "query").strip().lower()
    if send > 45.0 and transport != "a2a":
        warnings.append(
            f"send 预算 {send}s 已超过第二档的 45s，但 SQL_AGENT_TRANSPORT={transport}"
            f"（流式尚未承接流量）。契约 §1③「先能看到进度，再放宽超时」是硬要求："
            f"零反馈下放宽等待只是把空白屏拉长。"
        )
    return warnings


def run_startup_checks(env: Mapping[str, str] | None = None) -> None:
    """启动时逐条跑。任何一条硬约束不满足就抛，让进程起不来。"""
    check_security_config(env)
    for warning in check_timeout_budgets(env):
        logger.warning("启动检查: %s", warning)
    if is_production(env):
        logger.info("启动检查: 生产模式安全配置完整")
