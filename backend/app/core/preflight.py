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

import ipaddress
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


# `/auth/token` 允许从哪些来源调用（见 token_issuer_networks）。
# 空 = 不限制，只在非生产模式下可接受。
TOKEN_ISSUER_CIDRS_VAR = "AI_HELPDESK_TOKEN_ISSUER_CIDRS"

# 允许一切等于没有约束。写成 0.0.0.0/0 往往是"为了让启动校验闭嘴"，
# 那比不配还糟：配置文件里看起来是有约束的。
_WILDCARD_NETWORKS = frozenset({"0.0.0.0/0", "::/0"})

# 部署范围。**没有默认值是故意的**：见 check_deployment_scope 的 docstring。
DEPLOYMENT_SCOPE_VAR = "AI_HELPDESK_DEPLOYMENT_SCOPE"
SCOPE_SINGLE_TENANT = "internal_single_tenant"
SCOPE_MULTI_TENANT = "multi_tenant"
VALID_SCOPES = frozenset({SCOPE_SINGLE_TENANT, SCOPE_MULTI_TENANT})


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


def token_issuer_networks(
    env: Mapping[str, str] | None = None,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """
    解析 `/auth/token` 的来源白名单。空列表 = 不限制。

    解析不了的条目**不忽略**，直接抛：一个拼错的 CIDR 静默变成"这条不算数"，
    等于白名单被悄悄放宽，而配置文件看起来是对的。
    """
    raw = _env(env).get(TOKEN_ISSUER_CIDRS_VAR, "").strip()
    if not raw:
        return []
    networks = []
    for item in (piece.strip() for piece in raw.split(",")):
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise InsecureDeploymentError(
                f"{TOKEN_ISSUER_CIDRS_VAR} 里的 {item!r} 不是合法的 CIDR：{exc}"
            ) from exc
    return networks


def check_token_issuer_binding(env: Mapping[str, str] | None = None) -> None:
    """
    生产模式下 `/auth/token` 必须绑定到一份来源白名单，否则拒绝启动。

    **为什么这条要写成代码而不是文档约定。** 这个端点按请求体里的 `user_id`
    签发身份，把签发权收敛到"持有管理 key 的调用方"只是过渡形态：管理 key 是
    一把**共享**钥匙，它证明的是"调用方是我们的基础设施"，不是"这个用户登录了"。
    真正该做这件事的是登录系统——本仓库里**不存在**这样一个组件
    （`grep -riE 'oidc|oauth|saml|sso' backend/` 无命中），对接方案写在
    `docs/auth-token-integration.md`。

    在那个组件出现之前，唯一还能收紧的是**可达范围**：这个端点只应该被网关
    调到。写进 README 是一句约定，约定不会在配错的时候报错——所以做成启动断言。

    白名单校验的是**直连对端**（`request.client.host`），不是 `X-Forwarded-For`：
    XFF 是请求头，任何人都能写。只有直连对端是传输层事实。代价是网关必须在
    白名单里而不是最终客户端——这正是想要的语义："只有网关能调到"。
    """
    if not is_production(env):
        return
    networks = token_issuer_networks(env)
    if not networks:
        raise InsecureDeploymentError(
            f"APP_ENV 是生产模式，但 {TOKEN_ISSUER_CIDRS_VAR} 未配置，拒绝启动：\n"
            "  - /auth/token 会按请求体里的 user_id 签发身份，它只应该被网关调到。\n"
            "    配上网关所在的网段，例如 "
            f"{TOKEN_ISSUER_CIDRS_VAR}=10.0.0.0/8,127.0.0.1/32。\n"
            "  - 这一步是过渡形态。长期该由登录系统签发令牌，"
            "对接方案见 docs/auth-token-integration.md。"
        )
    wildcards = [str(n) for n in networks if str(n) in _WILDCARD_NETWORKS]
    if wildcards:
        raise InsecureDeploymentError(
            f"{TOKEN_ISSUER_CIDRS_VAR} 里有 {', '.join(wildcards)}——允许一切等于"
            "没有约束，而配置文件看起来是有的。要么配真实网段，要么把这个变量留空"
            "并把 APP_ENV 改成非生产（那时这条校验不生效）。"
        )


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


def deployment_scope(env: Mapping[str, str] | None = None) -> str:
    """返回声明的部署范围；未声明时返回空串（由 check_deployment_scope 判定）。"""
    return _env(env).get(DEPLOYMENT_SCOPE_VAR, "").strip().lower()


def check_deployment_scope(env: Mapping[str, str] | None = None) -> None:
    """
    生产模式下必须**显式声明**这套部署的范围，否则拒绝启动。

    **为什么这条要存在。** 第一次上线定下的范围是「已认证 + 同权限
    内部人员 + 只读 + 有限库表 + 低并发 + 人工可核实」的试点。这个范围让三条
    原本的硬阻断降级为可接受：`/auth/token` 没接登录系统、下游最小授权没落到
    真实业务库、行范围/RLS 不启用。

    **但那三条降级只在系统确实跑在这个范围里时才成立。** 而在这条检查出现
    之前，"这是单租户部署"这件事只写在 README 和 `.env.example` 里——
    **一个不被代码强制的范围决定不是决定，是期望**。六个月后没人记得那一节，
    而它失效时不会有任何症状：系统照常工作，只是那三条降级的前提悄悄没了。

    **为什么不给默认值。** 给了默认值，"忘了配"和"想清楚了配成这个"就分不开，
    而这两件事的风险完全不同。同样的理由已经在别处用过一次：下游的
    `predicate: "*"` 是**显式声明**"这张表不做行隔离"，与"没配"是两回事。

    两个方向都要拦，这是本条的关键——**它不是一个"警告开关"，是一个双向断言**：

    - 声明单租户，却配了只有多租户才用得上的东西 ⇒ 有人以为这是多租户，拒绝启动；
    - 声明多租户，却缺了多租户必需的东西 ⇒ 隔离根本不成立，拒绝启动。
    """
    if not is_production(env):
        return
    env = _env(env)
    scope = deployment_scope(env)

    if not scope:
        raise InsecureDeploymentError(
            f"APP_ENV 是生产模式，但 {DEPLOYMENT_SCOPE_VAR} 未声明，拒绝启动：\n"
            f"  - {SCOPE_SINGLE_TENANT}：同权限内部人员、只读试点。"
            f"所有用户看到的数据范围**完全相同**，不做业务数据隔离。\n"
            f"  - {SCOPE_MULTI_TENANT}：不同用户只能看各自的数据。"
            f"这个范围要求下游按最终用户重新解析权限（见下）。\n"
            "  这个变量**故意没有默认值**：漏配和想清楚了配成某个值，风险完全不同。"
        )
    if scope not in VALID_SCOPES:
        raise InsecureDeploymentError(
            f"{DEPLOYMENT_SCOPE_VAR}={scope!r} 不是合法取值，"
            f"只能是 {sorted(VALID_SCOPES)} 之一。"
        )

    impersonate = _flag(env, "SQL_AGENT_IMPERSONATE")

    if scope == SCOPE_SINGLE_TENANT and impersonate:
        raise InsecureDeploymentError(
            f"{DEPLOYMENT_SCOPE_VAR}={SCOPE_SINGLE_TENANT} 与 "
            "SQL_AGENT_IMPERSONATE=true 矛盾，拒绝启动：\n"
            "  - impersonation 的用途是让下游按最终用户解析权限。"
            "同权限试点里所有人权限相同，它不解决任何问题；\n"
            "  - 打开它却声明单租户，说明有人**以为**这套系统在做数据隔离。"
            "那个误解本身就是风险——它会让人把系统用在范围之外。\n"
            f"  要做真隔离，把范围改成 {SCOPE_MULTI_TENANT} 并读那一档的要求。"
        )

    if scope == SCOPE_MULTI_TENANT and not impersonate:
        raise InsecureDeploymentError(
            f"{DEPLOYMENT_SCOPE_VAR}={SCOPE_MULTI_TENANT} 但 "
            "SQL_AGENT_IMPERSONATE 未开启，拒绝启动：\n"
            "  - 不传最终用户身份时，所有查询都以**服务身份**发出，"
            "下游拿到的权限对每个用户都一样——多租户隔离根本不成立，"
            "而链路看起来完全正常。\n"
            "  - 打开它只是第一步：下游还必须按最终用户重新解析策略并与服务身份"
            "取交集。本仓库校验不了那一侧，见 docs/integrations/sql-agent-identity-requirements.md。"
        )

    logger.info("启动检查: 部署范围 %s", scope)


def run_startup_checks(env: Mapping[str, str] | None = None) -> None:
    """启动时逐条跑。任何一条硬约束不满足就抛，让进程起不来。"""
    check_security_config(env)
    check_token_issuer_binding(env)
    check_deployment_scope(env)
    for warning in check_timeout_budgets(env):
        logger.warning("启动检查: %s", warning)
    if is_production(env):
        logger.info("启动检查: 生产模式安全配置完整")
