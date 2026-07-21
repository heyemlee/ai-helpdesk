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
SCRIPT = REPO / "backend" / "docker-deploy.sh"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file() or shutil.which("bash") is None,
    reason="需要 bash 和 backend/docker-deploy.sh",
)

# 假 docker：把每次调用的参数追加进日志，然后成功返回。
# `docker compose version` 也要成功，脚本才会选 COMPOSE_BIN=(docker compose)，
# 于是所有 compose 调用也从这里过。
_FAKE_DOCKER = """#!/bin/bash
echo "$@" >> "$CMDLOG"
# psql 那一步脚本会重定向 stdin，读掉免得管道破裂
if [[ "$*" == *psql* ]]; then cat > /dev/null; fi
exit 0
"""


@pytest.fixture
def fake_repo(tmp_path):
    """
    在临时目录里搭一个仓库形状，**绝不能对真仓库跑 restore**——
    它会 `cp` 覆盖 backend/.env 和 backend/config。
    """
    root = tmp_path / "repo"
    backend = root / "backend"
    (backend / "config").mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n")
    (backend / ".env").write_text("APP_ENV=development\n")
    shutil.copy(SCRIPT, backend / "docker-deploy.sh")

    backup = tmp_path / "backup"
    (backup / "config").mkdir(parents=True)
    (backup / "postgres.sql").write_text("-- dump\n")
    (backup / "dump.rdb").write_bytes(b"redis")
    (backup / ".env").write_text("APP_ENV=production\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(_FAKE_DOCKER)
    docker.chmod(0o755)

    return {"root": root, "backend": backend, "backup": backup, "bin": bindir,
            "cmdlog": tmp_path / "cmds.log"}


def _run_restore(fake_repo) -> list[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
    env["CMDLOG"] = str(fake_repo["cmdlog"])
    proc = subprocess.run(
        ["bash", str(fake_repo["backend"] / "docker-deploy.sh"),
         "restore", str(fake_repo["backup"])],
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
        """恢复出来的配置要真的落到 backend/ 下，别只在日志里说了一句。"""
        _run_restore(fake_repo)
        assert (fake_repo["backend"] / ".env").read_text() == "APP_ENV=production\n"
        assert (fake_repo["backend"] / "config").is_dir()

    def test_it_says_it_finished(self, fake_repo):
        """跑通的那条路要有明确结束标志——半途失败时不该出现。"""
        env = dict(os.environ)
        env["PATH"] = f"{fake_repo['bin']}:{env['PATH']}"
        env["CMDLOG"] = str(fake_repo["cmdlog"])
        proc = subprocess.run(
            ["bash", str(fake_repo["backend"] / "docker-deploy.sh"),
             "restore", str(fake_repo["backup"])],
            input="y\n", capture_output=True, text=True, env=env, timeout=120,
        )
        assert "恢复完成" in proc.stdout


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
            ["bash", str(fake_repo["backend"] / "docker-deploy.sh"),
             "restore", str(fake_repo["backup"])],
            input="y\n", capture_output=True, text=True, env=env, timeout=120,
        )
        assert "恢复完成" not in proc.stdout, "Postgres 恢复失败了却报告成功"
        assert "恢复失败" in proc.stdout or proc.returncode != 0
