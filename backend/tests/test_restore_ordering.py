"""
恢复流程的命令顺序。

原来的 `restore_data` 先 `compose stop` 停掉**全部**服务，再往**已经停掉**的
Postgres 容器里 `docker exec psql`——exec 要求容器在跑，那一步必然失败。

后果不是"静默成功"（`set -e` 会在那里中止，退出码非零），而是**恢复停在半路**：
Redis 的 rdb 已经换掉了，Postgres 一行没进，配置没恢复，服务全停着。
两个存储从此对不上，而现场看起来只是"命令报了个错"。

真正的验证是在空环境跑一次完整恢复（本轮没做，Docker Hub 从这台机器拉不动，
见交付说明）。这一组做的是它下面那一层：**用假的 docker 跑一遍，把命令序列
录下来，断言顺序对**。它验不了"数据真的恢复了"，但能挡住"顺序又被改回去"，
而顺序正是上一次出错的地方。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "docker-deploy.sh"
COMPOSE = REPO / "docker-compose.yml"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file() or shutil.which("bash") is None,
    reason="需要 bash 和 scripts/docker-deploy.sh",
)

# 假 docker：把每次调用的参数追加进日志，然后成功返回。
# `docker compose version` 也要成功，脚本才会选 COMPOSE_BIN=(docker compose)，
# 于是所有 compose 调用也从这里过。
_FAKE_DOCKER = """#!/bin/bash
echo "$@" >> "$CMDLOG"
# psql 那一步脚本会重定向 stdin，读掉免得管道破裂
if [[ "$*" == *psql* ]]; then cat > /dev/null; fi
# `docker inspect … --format` 要能给出卷名，否则脚本会判定"找不到 /data 卷"。
if [[ "$1" == "inspect" ]]; then echo "fake_redis_volume"; fi
# 健康检查：redis-cli ping 要回 PONG；FAKE_PG_DOWN 非空时 pg_isready 报"没有响应"。
if [[ "$*" == *redis-cli*ping ]]; then echo PONG; fi
if [[ "$*" == *pg_isready* && -n "$FAKE_PG_DOWN" ]]; then exit 2; fi
exit ${FAKE_DOCKER_EXIT:-0}
"""

# 假 curl：把要探的地址记下来，一个请求都不发。一行一个参数——地址要**原样**落盘，
# 拼坏了（比如端口后面带着空格和注释）得看得出来。
# FAKE_CURL_DOWN 是空格分隔的若干片段，地址里含其中任何一个就当它连不上
# （curl 连接被拒的退出码是 7）。
_FAKE_CURL = """#!/bin/bash
printf '%s\\n' "$@" >> "$CURLLOG"
for down in $FAKE_CURL_DOWN; do
    if [[ "$*" == *"$down"* ]]; then exit 7; fi
done
exit 0
"""

# 健康检查开头有一句 `sleep 10`（等服务起来），测试里不等。
_FAKE_SLEEP = """#!/bin/bash
exit 0
"""


@pytest.fixture
def fake_repo(tmp_path):
    """
    在临时目录里搭一个仓库形状，**绝不能对真仓库跑 restore**——
    它会 `cp` 覆盖 backend/.env 和 deploy/。
    """
    root = tmp_path / "repo"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (root / "deploy" / "nginx").mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n")
    (backend / ".env").write_text("APP_ENV=development\n")
    script = root / "scripts" / "docker-deploy.sh"
    script.parent.mkdir()
    shutil.copy(SCRIPT, script)

    backup = tmp_path / "backup"
    (backup / "deploy" / "prometheus").mkdir(parents=True)
    (backup / "deploy" / "prometheus" / "prometheus.yml").write_text("restored: true\n")
    (backup / "postgres.sql").write_text("-- dump\n")
    (backup / "dump.rdb").write_bytes(b"redis")
    (backup / ".env").write_text("APP_ENV=production\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)

    return {"root": root, "backend": backend, "backup": backup, "bin": bindir,
            "script": script, "cmdlog": tmp_path / "cmds.log"}


def _run_restore(fake_repo) -> list[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
    env["CMDLOG"] = str(fake_repo["cmdlog"])
    proc = subprocess.run(
        ["bash", str(fake_repo["script"]), "restore", str(fake_repo["backup"])],
        input="y\n", capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, f"恢复脚本退出码 {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    log = fake_repo["cmdlog"]
    return log.read_text().splitlines() if log.exists() else []


def _index_of(cmds: list[str], *needles: str) -> int:
    """第一条同时含有所有 needle 的命令的下标。找不到直接失败。"""
    for i, cmd in enumerate(cmds):
        if all(n in cmd for n in needles):
            return i
    pytest.fail(f"命令序列里找不到含 {needles} 的那条：\n" + "\n".join(cmds))


class TestRestoreOrdering:
    def test_postgres_is_restored_while_it_is_running(self, fake_repo):
        """
        **这是原来必然失败的那一步。** `docker exec psql` 要求容器在跑，
        所以在它之前不能有一条"把所有服务都停掉"的命令。
        """
        cmds = _run_restore(fake_repo)
        psql = _index_of(cmds, "psql")

        # 在 psql 之前，postgres 必须被拉起来
        up = _index_of(cmds, "up", "postgres")
        assert up < psql, "psql 跑在 postgres 起来之前"

        # 而且之前不能有无差别的 `compose stop`（不带服务名 = 停全部）
        for cmd in cmds[:psql]:
            tail = cmd.split("stop", 1)[1].strip() if " stop" in f" {cmd}" else None
            assert tail is None or tail, f"psql 之前出现了停掉全部服务的命令: {cmd}"

    def test_the_app_is_stopped_first_and_started_last(self, fake_repo):
        """恢复期间应用不能还在写库；恢复完了要把它拉回来。"""
        cmds = _run_restore(fake_repo)
        stop_app = _index_of(cmds, "stop", "ai-helpdesk")
        psql = _index_of(cmds, "psql")
        start_app = _index_of(cmds, "start", "ai-helpdesk")

        assert stop_app < psql < start_app

    def test_the_customer_instance_is_stopped_and_started_too(self, fake_repo):
        """面向客户的实例也写同一个库：只停员工实例，恢复途中它还会继续写。"""
        cmds = _run_restore(fake_repo)
        stop_customer = _index_of(cmds, "stop", "ai-helpdesk-customer")
        psql = _index_of(cmds, "psql")
        start_customer = _index_of(cmds, "start", "ai-helpdesk-customer")

        assert stop_customer < psql < start_customer

    def test_redis_is_stopped_around_the_rdb_copy(self, fake_repo):
        """
        跑着的 Redis 不读磁盘上的 rdb，退出时还会用内存里的数据把它覆盖掉
        ——照原样 `cp` 进去等于什么都没做。所以必须停下来换、再起。
        """
        cmds = _run_restore(fake_repo)
        stop_redis = _index_of(cmds, "stop", "redis")
        copy = _index_of(cmds, "cp", "dump.rdb")
        start_redis = _index_of(cmds, "start", "redis")

        assert stop_redis < copy < start_redis

    def test_config_and_env_land_inside_the_repo(self, fake_repo):
        """恢复出来的配置要真的落到仓库里，别只在日志里说了一句：.env 进 backend/，基础设施配置整体替换 deploy/。"""
        _run_restore(fake_repo)
        assert (fake_repo["backend"] / ".env").read_text() == "APP_ENV=production\n"
        deploy = fake_repo["root"] / "deploy"
        assert (deploy / "prometheus" / "prometheus.yml").read_text() == "restored: true\n"
        # 整体替换，不是叠加：恢复前就有、备份里没有的文件不该留下来
        assert not (deploy / "nginx").exists()

    def test_customer_instance_key_is_restored_when_backed_up(self, fake_repo):
        """面向客户实例的密钥文件在备份里就要恢复：compose 要求它存在，缺了客户实例起不来。"""
        (fake_repo["backup"] / ".env.customer").write_text("AI_HELPDESK_API_KEY=k\n")
        _run_restore(fake_repo)
        assert (fake_repo["backend"] / ".env.customer").read_text() == "AI_HELPDESK_API_KEY=k\n"

    def test_legacy_backup_with_flat_config_dir_restores_env_but_not_deploy(self, fake_repo):
        """
        目录重整之前的备份把配置存在 config/（扁平布局，当时在 backend/config/）。
        它和 deploy/ 的按服务分目录布局对不上，脚本必须**不动 deploy/**、只恢复 .env，
        并且说清楚——而不是把旧布局整个盖上去。
        """
        backup = fake_repo["backup"]
        shutil.rmtree(backup / "deploy")
        (backup / "config").mkdir()
        (backup / "config" / "prometheus.yml").write_text("legacy: true\n")
        env = dict(os.environ)
        env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
        env["CMDLOG"] = str(fake_repo["cmdlog"])
        proc = subprocess.run(
            ["bash", str(fake_repo["script"]), "restore", str(backup)],
            input="y\n", capture_output=True, text=True, env=env, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert (fake_repo["backend"] / ".env").read_text() == "APP_ENV=production\n"
        assert (fake_repo["root"] / "deploy" / "nginx").is_dir(), "deploy/ 不该被旧布局覆盖"
        assert not (fake_repo["root"] / "deploy" / "prometheus.yml").exists()
        assert "旧格式备份" in proc.stdout

    def test_it_says_it_finished(self, fake_repo):
        """跑通的那条路要有明确结束标志——半途失败时不该出现。"""
        env = dict(os.environ)
        env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
        env["CMDLOG"] = str(fake_repo["cmdlog"])
        proc = subprocess.run(
            ["bash", str(fake_repo["script"]), "restore", str(fake_repo["backup"])],
            input="y\n", capture_output=True, text=True, env=env, timeout=120,
        )
        assert "恢复完成" in proc.stdout


class TestRedisIsBackedUpAsAWholeDirectory:
    """
    恢复演练查出来的三条，逐条锁住。

    这一组存在的理由：那三条**全都不报错**。备份 `docker cp` 成功、脚本打印
    "备份完成"、恢复 `docker cp` 也成功——只是数据没动。演练之前它们在代码
    审查里活了两轮没被发现。
    """

    def test_backup_copies_the_whole_data_dir_not_just_dump_rdb(self):
        """
        缺陷一：Redis 跑 `--appendonly yes`，权威数据在 AOF 里。
        实测 appendonly.aof.1.incr.aof 9.7MB，dump.rdb **89 字节**（空 RDB）。
        旧写法两个方向都是空操作。
        """
        script = SCRIPT.read_text(encoding="utf-8")
        assert "docker cp ai-helpdesk-redis:/data/. " in script
        assert "docker cp ai-helpdesk-redis:/data/dump.rdb" not in script

    def test_pg_dump_can_restore_onto_a_non_empty_database(self):
        """
        缺陷二：裸 `pg_dump` 产出的 SQL 只有 CREATE 没有 DROP，
        往非空库灌会在第一张表就 `already exists`。
        而**真实恢复场景恰恰是库非空**——旧写法只在空库上成立。
        """
        script = SCRIPT.read_text(encoding="utf-8")
        assert "pg_dump --clean --if-exists" in script

    def test_backup_dir_collision_renames_instead_of_merging(self):
        """
        缺陷三：目录名只到秒。同秒两次备份不是"覆盖"，是 `docker cp` 把第二份
        **嵌套**进第一份（dest 存在与否会改变 cp 的语义），恢复时读到陈旧数据。
        """
        script = SCRIPT.read_text(encoding="utf-8")
        assert 'backup_dir="${backup_dir}_$suffix"' in script

    def test_new_format_backup_replaces_the_whole_dir(self, fake_repo):
        """新格式备份：先用挂卷的临时容器清空，再拷整个目录。"""
        redis_data = fake_repo["backup"] / "redis-data" / "appendonlydir"
        redis_data.mkdir(parents=True)
        (redis_data / "appendonly.aof.manifest").write_text("x")
        cmds = _run_restore(fake_repo)
        joined = "\n".join(cmds)
        # 清空走的是 `docker run --rm -v <卷>:/data`，不是 `docker exec`——
        # 此刻 redis 已经 stop，exec 要求容器在跑。
        assert "run --rm -v fake_redis_volume:/data" in joined
        assert "cp" in joined and "redis-data/." in joined

    def test_legacy_backup_warns_that_redis_will_not_be_restored(self, fake_repo):
        """
        旧备份只有 dump.rdb。AOF 模式下 Redis 不读它——恢复不会生效。
        这种情况**必须明说**，否则用户以为两个存储都恢复了。
        """
        env = dict(os.environ)
        env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
        env["CMDLOG"] = str(fake_repo["cmdlog"])
        proc = subprocess.run(
            ["bash", str(fake_repo["script"]),
             "restore", str(fake_repo["backup"])],
            input="y\n", capture_output=True, text=True, env=env, timeout=120,
        )
        assert "不会" in proc.stdout and "Redis" in proc.stdout


class TestRestoreStopsOnFailure:
    def test_a_failing_psql_does_not_print_success(self, fake_repo):
        """
        Postgres 恢复失败时不能报告成功。

        这条在改之前的脚本上**也是绿的**（`set -e` 会在那里中止），所以它不是
        回归测试，是守卫：改动里加了显式的 `ON_ERROR_STOP=1` 和错误分支，
        这条盯住它们别被后来的重构绕过去。真正的回归测试是上面那条顺序断言。
        """
        docker = fake_repo["bin"] / "docker"
        docker.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$CMDLOG"\n'
            'if [[ "$*" == *psql* ]]; then cat > /dev/null; exit 1; fi\n'
            "exit 0\n"
        )
        docker.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
        env["CMDLOG"] = str(fake_repo["cmdlog"])
        proc = subprocess.run(
            ["bash", str(fake_repo["script"]),
             "restore", str(fake_repo["backup"])],
            input="y\n", capture_output=True, text=True, env=env, timeout=120,
        )
        assert "恢复完成" not in proc.stdout, "Postgres 恢复失败了却报告成功"
        assert "恢复失败" in proc.stdout or proc.returncode != 0


# ── 健康检查探的端口 ──────────────────────────────────────────────────────────

# compose 的一条端口映射："[绑定地址:]宿主机端口:容器端口"。
# 宿主机端口要么是数字，要么是 ${变量:-默认值}。
_PORT_MAPPING = re.compile(
    r'^\s*-\s*"(?:[\d.]+:)?(?P<host>\$\{(?P<var>\w+):-(?P<default>\d+)\}|(?P<literal>\d+)):\d+"',
    re.MULTILINE,
)


class _Published(NamedTuple):
    host: str         # compose 里的原文：${AI_HELPDESK_PORT:-8080} 或 80
    var: str | None   # 变量名；端口写死时是 None
    port: int         # 什么都不配时实际发布的宿主机端口


def _mappings(text: str) -> list[_Published]:
    return [
        _Published(m["host"], m["var"], int(m["default"] or m["literal"]))
        for m in _PORT_MAPPING.finditer(text)
    ]


def _published(service: str) -> _Published:
    """docker-compose.yml 里这个服务发布到宿主机的端口。"""
    compose = COMPOSE.read_text(encoding="utf-8")
    block = re.search(
        rf"^  {re.escape(service)}:\n(.*?)(?=^ {{0,2}}\S|\Z)", compose, re.MULTILINE | re.DOTALL
    )
    assert block, f"docker-compose.yml 里找不到服务 {service}"
    ports = _mappings(block.group(1))
    assert len(ports) == 1, f"docker-compose.yml 的 {service} 应该正好发布一个端口，实际: {ports}"
    return ports[0]


def _all_published_ports() -> set[int]:
    """compose 发布到宿主机的全部端口。"""
    return {p.port for p in _mappings(COMPOSE.read_text(encoding="utf-8"))}


def _script_code() -> str:
    """脚本里会执行的部分（整行注释去掉）。断言得落在代码上，不能被一句注释满足。"""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _health_check_body() -> str:
    body = re.search(r"^health_check\(\) \{\n(.*?)^\}", _script_code(), re.MULTILINE | re.DOTALL)
    assert body, "脚本里找不到 health_check()"
    return body.group(1)


@pytest.fixture
def fake_probes(fake_repo):
    """
    假 docker 旁边再放假 curl 和假 sleep。健康检查**不能真的去探跑测试的这台机器**：
    生产就部署在开发机上，探到的会是真服务，测试结果就取决于那一刻谁活着。
    """
    for name, body in (("curl", _FAKE_CURL), ("sleep", _FAKE_SLEEP)):
        exe = fake_repo["bin"] / name
        exe.write_text(body)
        exe.chmod(0o755)
    fake_repo["curllog"] = fake_repo["cmdlog"].with_name("curl.log")
    return fake_repo


# 健康检查该探的地址只有这一种形状：本机、可选端口、一个路径。
_PROBE_URL = re.compile(r"http://(?:localhost|127\.0\.0\.1)(?::(?P<port>\d+))?/\S*")


class _HealthRun(NamedTuple):
    returncode: int
    output: str         # stdout + stderr
    ports: list[int]    # curl 探过的宿主机端口，按先后
    docker: list[str]   # docker 命令序列


def _run_health(repo, command: str = "health", **env_overrides: str) -> _HealthRun:
    env = dict(os.environ)
    # 跑测试的 shell 里要是正好设了这两个变量，会盖掉要验的默认值
    env.pop("AI_HELPDESK_PORT", None)
    env.pop("AI_HELPDESK_CUSTOMER_PORT", None)
    env["PATH"] = f"{repo['bin']}:{env['PATH']}"
    env["CMDLOG"] = str(repo["cmdlog"])
    env["CURLLOG"] = str(repo["curllog"])
    env.update(env_overrides)
    for log in (repo["cmdlog"], repo["curllog"]):
        log.unlink(missing_ok=True)

    proc = subprocess.run(
        ["bash", str(repo["script"]), command],
        capture_output=True, text=True, env=env, timeout=120,
    )

    ports = []
    probed = repo["curllog"].read_text(encoding="utf-8").splitlines() if repo["curllog"].exists() else []
    for arg in probed:
        if arg.startswith("http"):
            url = _PROBE_URL.fullmatch(arg)
            assert url, f"健康检查探的地址不对: {arg!r}"
            ports.append(int(url["port"] or 80))
    docker = repo["cmdlog"].read_text().splitlines() if repo["cmdlog"].exists() else []
    return _HealthRun(proc.returncode, proc.stdout + proc.stderr, ports, docker)


class TestHealthCheckProbesWhatComposePublishes:
    """
    后来查出来的：`start` 末尾和 `health` 跑的健康检查，探的是两个过期端口。

    - 主应用探 `localhost:8000`。宿主机端口后来挪到了 8080，8000 上现在是同机的
      ai-sql-agent——它的 /health 是通的，所以 ai-helpdesk 挂没挂这里都报"主应用健康"。
    - Postgres 探 `localhost:8001/api/v1/heartbeat`。那是 ChromaDB 的心跳，存储换成
      Postgres 时只改了提示文字。compose 里没有任何服务发布 8001，所以永远报"不健康"。

    两条都**不报错**：脚本照常跑完，只是一条恒真、一条恒假。

    这一组的期望值都从 docker-compose.yml 读，**不在这里再写一遍 8080/8081**——
    写了就是第三份，会跟着另外两份一起漂。
    """

    def test_port_variables_are_spelled_exactly_as_in_compose(self):
        """变量名和默认值，脚本和 compose 必须一字不差：改了一边没改另一边，这条就红。"""
        code = _script_code()
        for service in ("ai-helpdesk", "ai-helpdesk-customer"):
            port = _published(service)
            assert port.var, f"compose 里 {service} 的宿主机端口不再是变量，脚本和这条测试要一起改"
            assert f'{port.var}="{port.host}"' in code, (
                f"compose 给 {service} 发布的宿主机端口是 {port.host}，脚本里的取值规则对不上"
            )
            assert f"${{{port.var}}}/health" in _health_check_body(), f"健康检查没有用 {port.var} 去探 {service}"

    def test_every_http_probe_goes_to_a_port_compose_publishes(self):
        """
        health_check 里每一个 curl 的端口，都得是 compose 真的发布到宿主机的端口。
        8000（只在容器内）和 8001（没有任何服务）都过不了这条。
        """
        defaults = {p.var: p.port for p in _mappings(COMPOSE.read_text(encoding="utf-8")) if p.var}
        published = _all_published_ports()
        urls = re.findall(r'http://[^\s"]+', _health_check_body())
        assert urls, "health_check 里一个 HTTP 探测都没找到"
        for url in urls:
            for var in re.findall(r"\$\{(\w+)\}", url):
                assert var in defaults, f"{url} 用了 compose 端口映射里没有的变量 {var}"
            resolved = urlsplit(re.sub(r"\$\{(\w+)\}", lambda m: str(defaults[m[1]]), url))
            assert resolved.hostname in ("localhost", "127.0.0.1"), url
            assert (resolved.port or 80) in published, f"{url} 探的端口 compose 没有发布到宿主机"

    def test_postgres_is_not_probed_over_http(self):
        """Postgres 不讲 HTTP。在容器里跑 pg_isready，不猜宿主机端口。"""
        assert "exec -T postgres pg_isready" in _health_check_body()
        assert "heartbeat" not in _script_code()

    def test_it_probes_the_published_ports_and_reports_everything_healthy(self, fake_probes):
        """什么都不配时探的是 compose 的默认端口；全都应答时六行全绿。"""
        run = _run_health(fake_probes)
        assert run.returncode == 0, run.output
        for service in ("ai-helpdesk", "ai-helpdesk-customer", "nginx"):
            assert _published(service).port in run.ports, f"没有探 {service} 发布的端口，探的是 {run.ports}"
        assert set(run.ports) <= _all_published_ports(), f"探了 compose 没有发布的端口: {run.ports}"
        for line in ("✓ 主应用健康", "✓ 客户实例健康", "✓ Nginx 入口健康",
                     "✓ Redis 健康", "✓ Postgres 健康", "✓ Prometheus 健康"):
            assert line in run.output
        assert "✗" not in run.output

    @pytest.mark.parametrize("service, verdict", [
        pytest.param("ai-helpdesk", "✗ 主应用不健康", id="staff"),
        pytest.param("ai-helpdesk-customer", "✗ 客户实例不健康", id="customer"),
        pytest.param("nginx", "✗ Nginx 入口不健康", id="nginx"),
    ])
    def test_each_line_reports_the_service_it_names(self, fake_probes, service, verdict):
        """compose 里哪个服务的端口连不上，报不健康的就得正好是说它的那一行，别的不受牵连。"""
        run = _run_health(fake_probes, FAKE_CURL_DOWN=f":{_published(service).port}/")
        assert verdict in run.output
        assert run.output.count("✗") == 1, run.output

    def test_a_neighbour_answering_on_another_port_is_not_our_app(self, fake_probes):
        """
        **出事时的形态。** 让 compose 发布的端口全部连不上，其余端口照常应答
        （本机 8000 上的 ai-sql-agent 就是这样一个"其余"）：三个入口都必须报不健康。
        旧脚本在这里报"主应用健康"。
        """
        down = " ".join(f":{port}/" for port in _all_published_ports())
        run = _run_health(fake_probes, FAKE_CURL_DOWN=down)
        for line in ("✗ 主应用不健康", "✗ 客户实例不健康", "✗ Nginx 入口不健康"):
            assert line in run.output
        for line in ("✓ 主应用健康", "✓ 客户实例健康", "✓ Nginx 入口健康"):
            assert line not in run.output

    def test_postgres_verdict_follows_pg_isready(self, fake_probes):
        """pg_isready 说行就报健康，说不行就报不健康。旧脚本两种情况都报不健康。"""
        up = _run_health(fake_probes)
        _index_of(up.docker, "exec -T postgres pg_isready")
        assert "✓ Postgres 健康" in up.output

        down = _run_health(fake_probes, FAKE_PG_DOWN="1")
        assert "✗ Postgres 不健康" in down.output
        assert "✓ Postgres 健康" not in down.output

    def test_ports_exported_in_the_shell_are_honoured(self, fake_probes):
        """compose 认 shell 里的 AI_HELPDESK_PORT / AI_HELPDESK_CUSTOMER_PORT，脚本也得认。"""
        run = _run_health(fake_probes, AI_HELPDESK_PORT="18080", AI_HELPDESK_CUSTOMER_PORT="18081")
        assert 18080 in run.ports and 18081 in run.ports
        assert _published("ai-helpdesk").port not in run.ports
        assert _published("ai-helpdesk-customer").port not in run.ports

    def test_ports_set_in_the_root_env_file_are_honoured(self, fake_probes):
        """
        compose 做变量替换时还读仓库根的 .env，`.env.example` 教的也是在那里改端口。
        只认 shell 的话，照文档改了端口，compose 换了地方发布，脚本还在探默认端口
        ——同一个故障换个入口再来一次。
        """
        (fake_probes["root"] / ".env").write_text(
            "REDIS_PASSWORD=aihelpdesk123\n"
            "AI_HELPDESK_PORT=18080\n"
            'AI_HELPDESK_CUSTOMER_PORT="18081"  # 客户实例\n',
            encoding="utf-8",
        )
        run = _run_health(fake_probes)
        assert 18080 in run.ports and 18081 in run.ports
        assert _published("ai-helpdesk").port not in run.ports
        assert _published("ai-helpdesk-customer").port not in run.ports

    @pytest.mark.parametrize("line", [
        pytest.param("AI_HELPDESK_PORT=18080", id="plain"),
        pytest.param('AI_HELPDESK_PORT="18080"', id="double-quoted"),
        pytest.param("AI_HELPDESK_PORT='18080'", id="single-quoted"),
        pytest.param("export AI_HELPDESK_PORT=18080", id="export"),
        pytest.param("AI_HELPDESK_PORT=18080  # 员工实例", id="inline-comment"),
        pytest.param("AI_HELPDESK_PORT = 18080", id="spaces"),
        pytest.param("AI_HELPDESK_PORT: 18080", id="yaml-colon"),
        pytest.param("AI_HELPDESK_PORT=1\nAI_HELPDESK_PORT=18080", id="last-one-wins"),
    ])
    def test_root_env_file_spellings_that_compose_accepts(self, fake_probes, line):
        """
        这几种写法 compose 都认（拿 `docker compose config` 逐条对照过取值）。
        脚本少认一种，就是 compose 换了端口而脚本没跟上。
        """
        (fake_probes["root"] / ".env").write_text(
            f"REDIS_PASSWORD=aihelpdesk123\n{line}\nGRAFANA_USER=admin\n", encoding="utf-8"
        )
        run = _run_health(fake_probes)
        assert 18080 in run.ports, f"探的是 {run.ports}"
        assert _published("ai-helpdesk").port not in run.ports

    def test_commented_out_and_lookalike_lines_in_the_root_env_file_do_not_count(self, fake_probes):
        """注释掉的行和名字只是相像的变量，compose 不当回事，脚本也不能当回事。"""
        (fake_probes["root"] / ".env").write_text(
            "# AI_HELPDESK_PORT=18080\n"
            "AI_HELPDESK_PORT_OLD=18081\n"
            "MY_AI_HELPDESK_PORT=18082\n"
        )
        run = _run_health(fake_probes)
        assert _published("ai-helpdesk").port in run.ports
        assert not {18080, 18081, 18082} & set(run.ports)

    def test_the_shell_wins_over_the_root_env_file(self, fake_probes):
        """两边都设了，compose 以 shell 为准；脚本取反了就会探到 compose 没用的那个端口。"""
        (fake_probes["root"] / ".env").write_text("AI_HELPDESK_PORT=18080\n")
        run = _run_health(fake_probes, AI_HELPDESK_PORT="28080")
        assert 28080 in run.ports
        assert 18080 not in run.ports

    def test_start_ends_with_the_same_checks(self, fake_probes):
        """`start` 起完服务后跑的是同一套检查，不是另一份端口。"""
        started = _run_health(fake_probes, command="start")
        up = _index_of(started.docker, "up -d")
        pg_isready = _index_of(started.docker, "exec -T postgres pg_isready")
        assert up < pg_isready
        assert started.ports == _run_health(fake_probes).ports
