"""
有界连接池。

以前 `PostgresMCPExecutor` 每次执行都现建连接：握手开销被乘上 QPS，
而且连接数没有上限——业务库的 max_connections 由客户端的心情决定。
"""
from __future__ import annotations

import unittest

from ai_sql_agent.execution.pool import BoundedConnectionPool


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class BoundedConnectionPoolTest(unittest.TestCase):
    def setUp(self):
        self.built = []

        def factory():
            conn = _FakeConn()
            self.built.append(conn)
            return conn

        self.factory = factory

    def test_connection_is_reused(self):
        pool = BoundedConnectionPool(self.factory, max_size=2)
        conn, pooled = pool.acquire()
        pool.release(conn, pooled)
        again, _ = pool.acquire()
        self.assertIs(again, conn)
        self.assertEqual(len(self.built), 1)

    def test_pool_never_exceeds_its_bound(self):
        pool = BoundedConnectionPool(self.factory, max_size=2)
        held = [pool.acquire() for _ in range(5)]
        for conn, pooled in held:
            pool.release(conn, pooled)
        self.assertLessEqual(len(pool._idle), 2)
        # 超出上限的那些必须真的关掉，不能只是丢引用
        self.assertEqual(sum(1 for c in self.built if c.closed), 3)

    def test_broken_connection_is_not_returned_to_the_pool(self):
        """
        出过错的连接可能停在半死的事务里。还回去的话，下一个人会收到
        一条与自己无关的错误——最难排查的那种。
        """
        pool = BoundedConnectionPool(self.factory, max_size=2)
        conn, pooled = pool.acquire()
        pool.release(conn, pooled, reusable=False)
        self.assertTrue(conn.closed)
        self.assertEqual(len(pool._idle), 0)

    def test_closed_connection_is_discarded(self):
        pool = BoundedConnectionPool(self.factory, max_size=2)
        conn, pooled = pool.acquire()
        conn.closed = True
        pool.release(conn, pooled)
        self.assertEqual(len(pool._idle), 0)

    def test_idle_connections_expire(self):
        pool = BoundedConnectionPool(self.factory, max_size=2, idle_seconds=-1)
        conn, pooled = pool.acquire()
        pool.release(conn, pooled)
        fresh, _ = pool.acquire()
        self.assertIsNot(fresh, conn)
        self.assertTrue(conn.closed, "过期连接要真的关掉")

    def test_disabled_pool_always_builds_a_fresh_connection(self):
        """max_size=0 = 回到 2026-09-04 之前的行为，留个退路。"""
        pool = BoundedConnectionPool(self.factory, max_size=0)
        self.assertFalse(pool.enabled)
        conn, pooled = pool.acquire()
        self.assertFalse(pooled)
        pool.release(conn, pooled)
        self.assertTrue(conn.closed)

    def test_close_all_releases_everything(self):
        pool = BoundedConnectionPool(self.factory, max_size=3)
        for conn, pooled in [pool.acquire() for _ in range(3)]:
            pool.release(conn, pooled)
        pool.close_all()
        self.assertTrue(all(c.closed for c in self.built))


class ExecutorWiringTest(unittest.TestCase):
    def test_postgres_executor_builds_a_pool_from_settings(self):
        from ai_sql_agent.execution import PostgresMCPExecutor

        executor = PostgresMCPExecutor("db", "postgresql://x/y", pool_max_connections=3)
        self.assertTrue(executor._pool.enabled)
        self.assertEqual(executor._pool._max_size, 3)
        executor.close()


if __name__ == "__main__":
    unittest.main()
