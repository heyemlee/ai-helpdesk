"""
docker-compose.yml 里只靠配置守住的两条边界。

这两条都没有代码替它兜底：compose 写错了，服务照样起得来、照样能用，只是边界
没了，而且没有任何症状。所以直接断言配置本身。

1. **对外只有 nginx 的 80。** 员工实例没有 API key（控制台不发 X-API-Key），
   它和控制台的端口一旦绑到所有网卡，同一网络里的任何设备都能读工单、发起订单
   查询。之前就是这样。
2. **客户实例碰不到员工那边的数据。** 靠两个环境变量：不接 ai-sql-agent，
   不用跨会话记忆（见 test_cross_session_memory_switch.py）。
"""
import re
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="需要仓库根的 docker-compose.yml")


def _list_entries(key: str) -> dict[str, list[str]]:
    """
    每个服务下某个列表键（ports / environment）的条目。

    逐行读，不解析 YAML：这份文件的缩进是固定的（服务名 2 格、服务的键 4 格），
    不值得为两条用例给后端加一个 YAML 依赖。条目读不出来时下面的断言会失败，
    不会悄悄通过。
    """
    entries: dict[str, list[str]] = {}
    service, inside = None, False
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"  ([\w-]+):\s*$", line):
            service, inside = m.group(1), False
        elif re.match(r"    [\w-]+:", line):
            inside = line.strip() == f"{key}:"
        elif inside and (m := re.match(r'\s+- "?([^"]*?)"?\s*$', line)):
            entries.setdefault(service, []).append(m.group(1))
    return entries


class TestOnlyTheCustomerEntryIsReachableFromOtherMachines:
    def test_every_published_port_except_nginx_is_bound_to_loopback(self):
        ports = _list_entries("ports")
        assert "ai-helpdesk" in ports and "frontend" in ports, "没读到端口，这条用例没验到东西"

        open_to_the_network = sorted(
            f"{service} {port}"
            for service, published in ports.items()
            for port in published
            if not port.startswith("127.0.0.1:")
        )
        assert open_to_the_network == ["nginx 80:80"]


class TestCustomerInstanceIsCutOffFromStaffData:
    @pytest.fixture
    def env(self) -> dict[str, str]:
        entries = _list_entries("environment")["ai-helpdesk-customer"]
        return dict(entry.split("=", 1) for entry in entries)

    def test_it_does_not_delegate_to_the_sql_agent(self, env):
        """留空 = 委派整块不注册。必须是显式的空值，不写的话会从 backend/.env 继承。"""
        assert env["SQL_AGENT_URL"] == ""

    def test_it_holds_no_sql_agent_key(self, env):
        """员工实例的 Key 也在 backend/.env 里：客户实例这里清空，网络和 Key 两道隔离各自成立。"""
        assert env["SQL_AGENT_API_KEY"] == ""

    def test_it_keeps_no_cross_session_memory(self, env):
        assert env["ENABLE_CROSS_SESSION_MEMORY"] == "false"


class TestEventLogIsSplitByInstance:
    """
    事件日志两个实例共用一张表，按 AI_HELPDESK_INSTANCE 分开。没设实例时日志不开——宁可
    不记，也不能标错：标错了，员工那边就看得到客户的轮次。客户实例的读接口要关，原文也留得短。
    开关本身不在 compose 里：写在这里会压过 backend/.env，就只能改 compose 才关得掉。
    """

    @pytest.fixture
    def envs(self) -> dict[str, dict[str, str]]:
        entries = _list_entries("environment")
        return {
            service: dict(e.split("=", 1) for e in entries[service])
            for service in ("ai-helpdesk", "ai-helpdesk-customer")
        }

    def test_each_backend_says_which_instance_it_is(self, envs):
        assert envs["ai-helpdesk"]["AI_HELPDESK_INSTANCE"] == "staff"
        assert envs["ai-helpdesk-customer"]["AI_HELPDESK_INSTANCE"] == "customer"

    def test_the_switch_is_left_to_the_env_files(self, envs):
        for service in envs.values():
            assert "AI_HELPDESK_EVENT_LOG" not in service

    def test_the_customer_log_cannot_be_read_back_and_keeps_text_for_two_weeks(self, envs):
        customer = envs["ai-helpdesk-customer"]
        assert customer["AI_HELPDESK_EVENT_LOG_READ"] == "off"
        assert customer["AI_HELPDESK_EVENT_LOG_BODY_DAYS"] == "14"

    def test_the_services_read_the_settings_the_way_they_were_meant(self, envs):
        """backend/.env 打开日志之后，按服务真正读配置的那段代码解析一遍：写错一个取值，日志会悄悄不开。"""
        from app.events import EventLogConfig

        staff = EventLogConfig.from_env({**envs["ai-helpdesk"], "AI_HELPDESK_EVENT_LOG": "full"})
        customer = EventLogConfig.from_env({**envs["ai-helpdesk-customer"], "AI_HELPDESK_EVENT_LOG": "full"})
        assert (staff.mode, staff.instance, staff.read, staff.body_days, staff.row_days) == (
            "full", "staff", True, 30, 180)
        assert (customer.mode, customer.instance, customer.read, customer.body_days,
                customer.row_days) == ("full", "customer", False, 14, 180)
        assert staff.warnings == customer.warnings == ()


class TestOnlyTheStaffSideReachesTheSqlAgent:
    """
    SQL Agent 的网络边界（docs 里的两道隔离：网络 + API Key）。网络这一道全靠下面这几行配置：
    写错了，服务照样起得来、照样能用，只是客户实例连得到 SQL Agent 了，而且没有任何症状。
    """

    @pytest.fixture
    def networks(self) -> dict[str, list[str]]:
        networks = _list_entries("networks")
        assert "sql-agent" in networks and "ai-helpdesk" in networks, "没读到网络，这组用例没验到东西"
        return networks

    def test_the_sql_agent_publishes_no_port(self):
        """发布了宿主机端口，任何容器都能经 host.docker.internal 连到它，网络隔离就白做了。"""
        ports = _list_entries("ports")
        assert "ai-helpdesk" in ports, "没读到端口，这条用例没验到东西"
        assert "sql-agent" not in ports

    def test_the_customer_instance_and_nginx_are_not_on_sql_net(self, networks):
        for service in ("ai-helpdesk-customer", "nginx"):
            assert "sql-net" not in networks[service], service

    def test_postgres_is_not_on_sql_net(self, networks):
        """两个实例都用超级用户连 postgres：挂上来的话，它就是从客户实例进 sql-net 的跳板。"""
        assert "sql-net" not in networks["postgres"]

    def test_the_console_is_only_on_sql_net(self, networks):
        """控制台背后是没有 Key 的员工实例（以后还替浏览器带 SQL Agent 的管理员 Key），客户实例不该连得到它。"""
        assert networks["frontend"] == ["sql-net"]

    def test_the_staff_instance_delegates_to_the_sql_agent_service_by_default(self, networks):
        env = dict(e.split("=", 1) for e in _list_entries("environment")["ai-helpdesk"])
        assert env["SQL_AGENT_URL"] == "${AI_HELPDESK_SQL_AGENT_URL:-http://sql-agent:8000}"
        assert "sql-net" in networks["ai-helpdesk"]

    def test_the_sql_agent_is_only_on_sql_net(self, networks):
        assert networks["sql-agent"] == ["sql-net"]

    def test_the_production_settings_are_pinned_in_compose(self):
        """
        写在 environment 里的值压过 env 文件，所以生产模式、鉴权、状态不进 Postgres 这几项改不动。
        生产模式下 SQL Agent 的就绪检查不过就不启动；开发模式下同样的问题只记一条警告。
        """
        env = dict(e.split("=", 1) for e in _list_entries("environment")["sql-agent"])
        assert env["AI_SQL_APP_ENV"] == "production"
        assert env["AI_SQL_DEPLOYMENT_SCOPE"] == "internal_single_tenant"
        assert env["AI_SQL_AUTH_MODE"] == "api_key"
        assert env["AI_SQL_PGVECTOR_DSN"] == ""


FRONTEND = COMPOSE.parent / "frontend"


class TestTheConsoleCarriesTheSqlAgentKeyServerSide:
    """
    控制台的 nginx 替浏览器带上 SQL Agent 的管理员 Key（合仓设计 §3.9，数据页面走 /sql-api/）。

    和上面几组一样只靠配置守着：写错了页面照样能用，只是 Key 跑到了浏览器看得到的地方（JS 包、
    docker inspect、compose 的解析结果），或者浏览器自己带的 Key 被原样转了过去。
    """

    @pytest.fixture
    def sql_api_block(self) -> str:
        template = (FRONTEND / "docker" / "templates" / "default.conf.template").read_text(encoding="utf-8")
        match = re.search(r"location /sql-api/ \{(.*?)\n    \}", template, re.S)
        assert match, "模板里没有 location /sql-api/"
        return match.group(1)

    def test_the_console_forwards_to_the_sql_agent_service(self):
        env = dict(e.split("=", 1) for e in _list_entries("environment")["frontend"])
        assert env["API_SQL_UPSTREAM"] == "sql-agent:8000"

    def test_the_key_is_never_an_environment_value(self):
        """compose 的 environment、Dockerfile 的 ENV 里都不该出现 Key：那两处 docker inspect 看得见。"""
        names = [e.split("=", 1)[0] for e in _list_entries("environment")["frontend"]]
        assert "API_SQL_KEY" not in names
        dockerfile = (FRONTEND / "Dockerfile").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(ENV|ARG)\b.*API_SQL_KEY", dockerfile, re.M)

    def test_only_the_console_key_directory_is_mounted(self):
        """deploy/secrets 整个挂进来的话，SQL Agent 的策略文件和员工那一把 Key 也跟着进了控制台。"""
        assert _list_entries("volumes")["frontend"] == ["./deploy/secrets/console:/run/secrets/console:ro"]

    def test_nginx_overwrites_whatever_key_the_browser_sends(self, sql_api_block):
        assert 'proxy_set_header X-API-Key "${API_SQL_KEY}";' in sql_api_block
        assert "set $sql_upstream \"${API_SQL_UPSTREAM}\";" in sql_api_block

    def test_only_requests_that_cannot_come_cross_site_get_the_key(self, sql_api_block):
        """
        Key 是服务端加的，别的网站能借员工的浏览器发"简单请求"过来（CSRF），以管理员身份跑到 SQL Agent 上。
        所以 /sql-api/ 要求控制台自己的请求头（跨站带它得先过 CORS 预检，nginx 不回），浏览器标明跨站的直接拒；
        整个控制台只认本机的 Host（DNS 重绑定时 Host 是攻击者的域名）。三条都在 Key 生效之前。
        """
        guards = sql_api_block.index('if ($http_x_console_request != "1")')
        assert '$http_sec_fetch_site ~* "^(cross-site|same-site)$"' in sql_api_block
        assert guards < sql_api_block.index("proxy_pass")
        template = (FRONTEND / "docker" / "templates" / "default.conf.template").read_text(encoding="utf-8")
        hosts = re.search(r"map \$host \$console_host_ok \{(.*?)\}", template, re.S)
        assert hosts, "没有 Host 白名单"
        allowed = {line.split()[0].strip('"') for line in hosts.group(1).splitlines()
                   if line.strip() and not line.strip().startswith("default")}
        assert allowed == {"127.0.0.1", "localhost", "[::1]"}
        assert "if ($console_host_ok = 0)" in template
        client = (FRONTEND / "src" / "api" / "sql" / "client.ts").read_text(encoding="utf-8")
        assert "'X-Console-Request': '1'" in client

    def test_the_stream_is_not_buffered_and_outlives_the_contract_limit(self, sql_api_block):
        """A2A 的 message/stream 是 SSE；契约里流的硬上限是 90 秒。"""
        assert "proxy_buffering off;" in sql_api_block
        timeout = re.search(r"proxy_read_timeout (\d+)s;", sql_api_block)
        assert timeout and int(timeout.group(1)) >= 120

    def test_the_key_loader_always_defines_the_variable(self):
        """
        变量没定义的话 envsubst 不替换，配置里留下字面量 ${API_SQL_KEY}，被当成 Key 发出去。
        envsubst 只放行 API_ 开头的变量；入口只 source 可执行的 .envsh。
        """
        loader = FRONTEND / "docker" / "entrypoint" / "16-sql-api-key.envsh"
        text = loader.read_text(encoding="utf-8")
        assert "export API_SQL_KEY" in text
        assert "/run/secrets/console/sql_agent_key" in text
        dockerfile = (FRONTEND / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY --chmod=0755 docker/entrypoint/16-sql-api-key.envsh /docker-entrypoint.d/" in dockerfile
        assert "NGINX_ENVSUBST_FILTER='^(API_|NGINX_LOCAL_RESOLVERS)'" in dockerfile
