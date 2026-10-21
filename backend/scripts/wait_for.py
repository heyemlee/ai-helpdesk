"""
等几个 TCP 端口都能连上再退出（k8s 的 initContainer 用）：Postgres、Redis 还没起来的时候，
api 和 worker 先起来只会连不上库、崩掉、被 kubelet 退避着重启几轮。

    python scripts/wait_for.py postgres:5432 redis:6379 --timeout 180
"""
import argparse
import socket
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="等 TCP 端口能连上")
    parser.add_argument("targets", nargs="+", help="host:port")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    for target in args.targets:
        host, _, port = target.rpartition(":")
        while True:
            try:
                with socket.create_connection((host, int(port)), timeout=2):
                    print(f"{target} 可以连了", flush=True)
                    break
            except OSError as ex:
                if time.monotonic() > deadline:
                    print(f"{target} 等了 {args.timeout:g} 秒还连不上: {ex}", file=sys.stderr)
                    return 1
                time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
