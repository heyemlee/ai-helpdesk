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

    def test_it_keeps_no_cross_session_memory(self, env):
        assert env["ENABLE_CROSS_SESSION_MEMORY"] == "false"
