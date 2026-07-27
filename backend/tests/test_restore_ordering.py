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
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "docker-deploy.sh"

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
exit ${FAKE_DOCKER_EXIT:-0}
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
