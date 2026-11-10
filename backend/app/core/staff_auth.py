"""Local employee accounts, revocable server sessions and live authorization.

The credential boundary is deliberately independent of legacy service keys.
An external SSO adapter must produce the same persisted, allowlisted accounts;
unsolicited identity headers and unconfigured SSO modes are never accepted.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from app.core.actor import ActorContext
from app.events.log import instance_from_env

current_actor: ContextVar[ActorContext | None] = ContextVar("staff_actor", default=None)
logger = logging.getLogger(__name__)
current_auth: ContextVar[StaffAuth | None] = ContextVar("staff_auth_service", default=None)
ROLES = {"employee", "operations", "admin"}
# How often an open response stream rechecks its staff session (seconds).
SESSION_RECHECK_S = float(os.getenv("AI_HELPDESK_SESSION_RECHECK_S", "5"))


def mode(env=None):
    env = os.environ if env is None else env
    return env.get("AI_HELPDESK_STAFF_AUTH", "local" if instance_from_env(env) == "staff" else "disabled").strip().lower()


DEMO_ADMIN_LOGIN, DEMO_ADMIN_PASSWORD = "AI_HELPDESK_DEMO_ADMIN_LOGIN", "AI_HELPDESK_DEMO_ADMIN_PASSWORD"
LOGIN_PATTERN = r"[a-z0-9][a-z0-9_.@-]{2,79}"
STRONG_MIN, DEMO_MIN = 14, 6
WEAK_SET, STRONG_SET = "weak_password_set", "strong_password_set"


def demo_admin(env=None):
    """(login, password) of the local-demo administrator, or None when not configured.

    Opt-in convenience for a demo on one machine: both variables set in the gitignored
    root .env. It is refused in production (check_config) and it relaxes the password
    minimum from 14 to 6, so it must never be set on a shared deployment.
    """
    env = os.environ if env is None else env
    login, password = env.get(DEMO_ADMIN_LOGIN, "").strip(), env.get(DEMO_ADMIN_PASSWORD, "")
    return (login, password) if login and password else None


def min_password_length(env=None):
    return DEMO_MIN if demo_admin(env) else STRONG_MIN


def check_demo_admin(env):
    login, password = env.get(DEMO_ADMIN_LOGIN, "").strip(), env.get(DEMO_ADMIN_PASSWORD, "")
    if not (login or password):
        return
    from app.core.preflight import is_production
    if is_production(env):
        raise ValueError("The demo administrator settings are not allowed in production")
    if bool(login) != bool(password):
        raise ValueError(f"Set both {DEMO_ADMIN_LOGIN} and {DEMO_ADMIN_PASSWORD}, or neither")
    if not re.fullmatch(LOGIN_PATTERN, login):
        raise ValueError(f"{DEMO_ADMIN_LOGIN} must be 3-80 characters: lowercase letters, digits and _ . @ -")
    if not DEMO_MIN <= len(password) <= 128:
        raise ValueError(f"{DEMO_ADMIN_PASSWORD} must be {DEMO_MIN}-128 characters")


def check_config(env):
    check_demo_admin(env)
    selected = mode(env)
    if selected not in {"local", "disabled"}:
        raise ValueError("Unsupported staff identity provider; SSO requires a configured, verified adapter")
    from app.core.preflight import is_production
    production = is_production(env)
    instance = instance_from_env(env)
    if env.get("AI_HELPDESK_INSTANCE") and not instance:
        raise ValueError("Invalid AI_HELPDESK_INSTANCE")
    if production and not instance:
        raise ValueError("Production requires an explicit staff or customer instance")
    if production and instance == "staff" and selected != "local":
        raise ValueError("Production staff authentication cannot be disabled")
    if selected == "local":
        if not env.get("POSTGRES_DSN") or not instance:
            raise ValueError("Staff authentication requires PostgreSQL and an explicit instance")
        origin = env.get("AI_HELPDESK_PUBLIC_ORIGIN", "http://localhost:5174")
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("PUBLIC_ORIGIN must be one exact origin without credentials or path")
        if production and (parsed.scheme != "https" or not env.get("AI_HELPDESK_PUBLIC_ORIGIN")):
            raise ValueError("Production employee sessions require an explicit HTTPS PUBLIC_ORIGIN")


def hash_password(password: str) -> str:
    minimum = min_password_length()
    if not minimum <= len(password) <= 128 or len(password.encode()) > 512:
        raise HTTPException(422, f"密码长度须为 {minimum}–128 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1, maxmem=256*1024*1024)
    return "scrypt:131072:8:1:" + salt.hex() + ":" + digest.hex()


def verify_password(password: str, encoded: str) -> bool:
    # Work factors are fixed, never taken as arbitrary resource requests from DB.
    try:
        algorithm, n, r, p, salt, expected = encoded.split(":")
        if (algorithm, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        salt_bytes = bytes.fromhex(salt)
        if len(salt_bytes) != 16 or len(password.encode()) > 512:
            return False
        digest = hashlib.scrypt(password.encode(), salt=salt_bytes, n=2**17, r=8, p=1, maxmem=256*1024*1024)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf(token):
    return hmac.new(token.encode(), b"helpdesk-csrf-v1", hashlib.sha256).hexdigest()


class StaffAuth:
    def __init__(self, db, instance, *, origin="http://localhost:5174", ttl_s=28800, idle_s=1800):
        self.db, self.instance, self.origin = db, instance, origin
        self.ttl_s, self.idle_s = ttl_s, idle_s
        self.secure = origin.startswith("https://")
        self.cookie = "__Host-helpdesk_session" if self.secure else "helpdesk_session"
        self._password_slots = asyncio.Semaphore(2)
        self._hash_tasks = set()

    async def password_work(self, function, *args):
        try:
            await asyncio.wait_for(self._password_slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(429, "登录繁忙，请稍后重试") from None
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._hash_tasks.add(task)

        def finished(done):
            self._hash_tasks.discard(done)
            self._password_slots.release()
            if not done.cancelled():
                done.exception()  # Consume errors even if the HTTP caller left.
        task.add_done_callback(finished)
        # Cancellation of HTTP must NOT release the slot while scrypt is running.
        return await asyncio.shield(task)

    async def administrator(self, actor, conn):
        if not isinstance(actor, ActorContext):
            raise HTTPException(401, "Verified administrator required")
        live = await self.provider(actor.actor_id, actor.instance, conn=conn)
        if live is None or live.auth_version != actor.auth_version or live.instance != self.instance:
            raise HTTPException(401, "Administrator authorization changed")
        live.require_role("admin")
        return live

    async def provider(self, actor_id, instance, *, conn=None):
        # Case commits hold this row until commit. Reuse their connection and do
        # not lock in a readonly consistent snapshot (Case GET).
        suffix = ""
        if conn is not None and await conn.fetchval("SHOW transaction_read_only") == "off":
            suffix = " FOR SHARE"
        row = await (conn or self.db).fetchrow(
            "SELECT * FROM staff_accounts WHERE actor_id=$1 AND instance=$2 AND active" + suffix,
            actor_id, instance)
        if not row:
            return None
        return ActorContext(row["actor_id"], frozenset({row["role"]}), row["instance"], row["auth_version"])

    async def audit(self, action, actor_id="", target="", *, conn=None):
        from app.core.staff_maintenance import AUTH_EVENTS
        AUTH_EVENTS.labels(action=action).inc()
        await (conn or self.db).execute("INSERT INTO staff_audit(action,actor_id,target) VALUES($1,$2,$3)", action, actor_id, target)

    async def create_account(self, login, password, role, *, by=None, bootstrap=False):
        if not re.fullmatch(LOGIN_PATTERN, login) or role not in ROLES:
            raise HTTPException(422, "账号或角色格式无效")
        encoded = await self.password_work(hash_password, password)
        actor_id = "emp_" + uuid.uuid4().hex
        async with self.db.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('staff-admin',0))")
            if not bootstrap:
                await self.administrator(by, conn)
            exists = await conn.fetchval("SELECT 1 FROM staff_accounts WHERE instance=$1 AND login=$2", self.instance, login)
            if exists:
                raise HTTPException(409, "账号已存在；不会自动替换密码")
            await conn.execute("INSERT INTO staff_accounts(actor_id,instance,login,password_hash,role) VALUES($1,$2,$3,$4,$5)",
                               actor_id, self.instance, login, encoded, role)
            await self.audit("account_created", by.actor_id if by else "deployment_bootstrap", actor_id, conn=conn)
            if len(password) < STRONG_MIN:
                await self.audit(WEAK_SET, by.actor_id if by else "deployment_bootstrap", actor_id, conn=conn)
        return {"actor_id": actor_id, "login": login, "role": role, "active": True}

    async def seed_demo_admin(self):
        """Create the configured demo administrator once; never touch an existing account."""
        configured = demo_admin()
        if configured is None:
            return None
        login, password = configured
        try:
            account = await self.create_account(login, password, "admin", bootstrap=True)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            row = await self.db.fetchrow("SELECT active, role, password_hash FROM staff_accounts WHERE instance=$1 AND login=$2",
                                         self.instance, login)
            if not row or not row["active"] or row["role"] != "admin":
                raise ValueError(f"{DEMO_ADMIN_LOGIN}={login} 对应的账号已存在，但不是启用的管理员："
                                 "换一个登录名，或先在控制台里把它改成启用的管理员") from None
            matches = await self.password_work(verify_password, password, row["password_hash"])
            logger.warning("演示管理员 %s 已存在，不改它的密码%s", login,
                           "" if matches else f"；注意 .env 里的 {DEMO_ADMIN_PASSWORD} 与它现在的密码不一致")
            return None
        logger.warning("已按 %s 创建演示管理员 %s（仅限本机演示，密码长度下限已放宽到 %d）",
                       DEMO_ADMIN_LOGIN, login, DEMO_MIN)
        return account

    async def change_account(self, target, *, by, active=None, role=None, password=None):
        if role is not None and role not in ROLES:
            raise HTTPException(422, "无效角色")
        encoded = await self.password_work(hash_password, password) if password is not None else None
        async with self.db.transaction() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('staff-admin',0))")
            await self.administrator(by, conn)
            row = await conn.fetchrow("SELECT * FROM staff_accounts WHERE actor_id=$1 AND instance=$2 FOR UPDATE", target, self.instance)
            if not row:
                raise HTTPException(404, "Not Found")
            if row["active"] and row["role"] == "admin" and (active is False or role not in {None, "admin"}):
                count = await conn.fetchval("SELECT count(*) FROM staff_accounts WHERE instance=$1 AND active AND role='admin'", self.instance)
                if count <= 1:
                    raise HTTPException(409, "不能停用或降级最后一个管理员")
            await conn.execute("UPDATE staff_accounts SET active=COALESCE($2,active),role=COALESCE($3,role),"
                               "password_hash=COALESCE($4,password_hash),auth_version=auth_version+1 WHERE actor_id=$1",
                               target, active, role, encoded)
            await conn.execute("DELETE FROM staff_sessions WHERE actor_id=$1", target)
            await self.audit("account_changed_sessions_revoked", by.actor_id, target, conn=conn)
            if password is not None:
                await self.audit(WEAK_SET if len(password) < STRONG_MIN else STRONG_SET, by.actor_id, target, conn=conn)

    async def revoke_weak_password_sessions(self):
        """On a production start, end the sessions of accounts whose password was set under the
        demo minimum, so a weak-password session cannot outlive the switch from demo mode.

        The marker is the latest WEAK_SET / STRONG_SET audit row of the account, so it needs no
        schema change. The login check refuses the same accounts afterwards.
        """
        from app.core.preflight import is_production
        if not is_production():
            return 0
        async with self.db.transaction() as conn:
            weak = [r["target"] for r in await conn.fetch(
                "SELECT target FROM (SELECT DISTINCT ON (target) target, action FROM staff_audit"
                " WHERE action IN ($1,$2) ORDER BY target, seq DESC) latest"
                " WHERE action=$1 AND target IN (SELECT actor_id FROM staff_accounts WHERE instance=$3)",
                WEAK_SET, STRONG_SET, self.instance)]
            if not weak:
                return 0
            revoked = await conn.fetchval("WITH gone AS (DELETE FROM staff_sessions WHERE actor_id = ANY($1::text[]) RETURNING 1)"
                                          " SELECT count(*) FROM gone", weak)
            if revoked:
                await conn.execute("UPDATE staff_accounts SET auth_version=auth_version+1 WHERE actor_id = ANY($1::text[])", weak)
                for actor_id in weak:
                    await self.audit("weak_password_sessions_revoked", "production_start", actor_id, conn=conn)
                logger.warning("生产模式启动：已结束 %d 个由演示最短密码账号建立的会话", revoked)
        return revoked

    async def login(self, login, password, peer):
        # Shared fixed-window_start counters BEFORE expensive password work. The peer
        # is the direct connection, never a client supplied forwarding header.
        async with self.db.transaction() as conn:
            for kind, value, limit in (("account", login, 6), ("peer", peer, 40)):
                bucket = token_hash(self.instance + ":" + kind + ":" + value)
                count = await conn.fetchval("INSERT INTO staff_login_limits(bucket,window_start,attempts)"
                    " VALUES($1,floor(extract(epoch FROM clock_timestamp())/60)::bigint,1)"
                    " ON CONFLICT(bucket) DO UPDATE SET window_start=excluded.window_start,attempts="
                    " CASE WHEN staff_login_limits.window_start=excluded.window_start THEN staff_login_limits.attempts+1 ELSE 1 END RETURNING attempts", bucket)
                if count > limit:
                    # Don't roll the counter back by raising inside the transaction.
                    blocked = True
                    break
            else:
                blocked = False
        if blocked:
            raise HTTPException(429, "登录尝试过多，请稍后重试", headers={"Retry-After": "60"})
        row = await self.db.fetchrow("SELECT * FROM staff_accounts WHERE instance=$1 AND login=$2", self.instance, login)
        dummy = "scrypt:131072:8:1:" + "00"*16 + ":" + "00"*64
        valid = await self.password_work(verify_password, password, row["password_hash"] if row else dummy)
        if not valid or not row or not row["active"]:
            await self.audit("login_denied")
            raise HTTPException(401, "账号或密码无效")
        from app.core.preflight import is_production
        if is_production() and len(password) < STRONG_MIN:
            # A password set while the demo minimum applied is not accepted in production,
            # whichever way it stayed in the database. Another administrator (or a new one
            # from scripts/staff_accounts.py) must set a strong one first.
            await self.audit("login_refused_weak_password", row["actor_id"])
            raise HTTPException(403, f"密码短于生产环境要求的 {STRONG_MIN} 位，请让管理员重置密码")
        token = secrets.token_urlsafe(32)
        async with self.db.transaction() as conn:
            fresh = await conn.fetchrow("SELECT * FROM staff_accounts WHERE actor_id=$1 AND active FOR SHARE", row["actor_id"])
            if not fresh or fresh["auth_version"] != row["auth_version"]:
                raise HTTPException(401, "账号或密码无效")
            await conn.execute("INSERT INTO staff_sessions(token_hash,actor_id,auth_version,expires_at)"
                               " VALUES($1,$2,$3,now()+$4*interval '1 second')",
                               token_hash(token), row["actor_id"], row["auth_version"], self.ttl_s)
            await self.audit("login", row["actor_id"], conn=conn)
        return token

    async def session(self, token, *, touch=False):
        if not token or len(token) > 128:
            raise HTTPException(401, "请使用员工账号登录")
        row = await self.db.fetchrow("SELECT a.* FROM staff_sessions s JOIN staff_accounts a USING(actor_id)"
            " WHERE s.token_hash=$1 AND a.instance=$2 AND a.active AND s.auth_version=a.auth_version"
            " AND s.expires_at>clock_timestamp() AND s.last_seen>clock_timestamp()-$3*interval '1 second'",
            token_hash(token), self.instance, self.idle_s)
        if not row:
            raise HTTPException(401, "会话已失效，请重新登录")
        if touch:
            await self.db.execute("UPDATE staff_sessions SET last_seen=now() WHERE token_hash=$1", token_hash(token))
        return ActorContext(row["actor_id"], frozenset({row["role"]}), row["instance"], row["auth_version"])

    async def logout(self, actor):
        # Logout invalidates all sessions AND already queued work for this account.
        async with self.db.transaction() as conn:
            live = await conn.fetchrow("SELECT auth_version FROM staff_accounts WHERE actor_id=$1 AND instance=$2 AND active FOR UPDATE", actor.actor_id, actor.instance)
            if live is None or live["auth_version"] != actor.auth_version:
                raise HTTPException(401, "Session revoked")
            await conn.execute("UPDATE staff_accounts SET auth_version=auth_version+1 WHERE actor_id=$1", actor.actor_id)
            await conn.execute("DELETE FROM staff_sessions WHERE actor_id=$1", actor.actor_id)
            await self.audit("logout_all_sessions", actor.actor_id, conn=conn)

    async def conversation(self, actor, conv_id, *, conn=None):
        owner = await (conn or self.db).fetchval("SELECT actor_id FROM staff_conversations WHERE instance=$1 AND conv_id=$2",
                                               actor.instance, conv_id)
        if owner != actor.actor_id:
            raise HTTPException(404, "会话不存在或不属于当前员工")

    async def new_conversation(self, actor):
        conv_id = str(uuid.uuid4())
        await self.db.execute("INSERT INTO staff_conversations(instance,conv_id,actor_id) VALUES($1,$2,$3)",
                              actor.instance, conv_id, actor.actor_id)
        return conv_id


def role_for(path, method):
    if path.startswith(("/admin", "/eval", "/monitor")) or path in {"/auth/token", "/skills/reload", "/knowledge/add", "/knowledge/upload"}:
        return "admin"
    if path.startswith("/knowledge/documents/") and method not in {"GET", "HEAD"}:
        return "admin"
    if path != "/tickets/cases" and re.fullmatch(r"/tickets(?:/[^/]+(?:/status)?)?", path):
        return "operations"
    return "employee"


class StaffSessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        auth = getattr(request.app.state, "staff_auth", None)
        from app.core.preflight import is_production
        selected = mode()
        invalid = selected not in {"local", "disabled"} or (is_production() and (not instance_from_env() or (instance_from_env() == "staff" and selected != "local")))
        if invalid:
            return await JSONResponse({"detail": "身份配置无效"}, status_code=503)(scope, receive, send)
        if auth is None:
            if selected == "local" and request.url.path not in {"/livez", "/readyz"}:
                return await JSONResponse({"detail": "员工身份服务尚未就绪"}, status_code=503)(scope, receive, send)
            return await self.app(scope, receive, send)
        path, method = request.url.path, request.method
        if path in {"/livez", "/readyz"}:
            return await self.app(scope, receive, send)
        if path == "/metrics":
            key_path = os.getenv("AI_HELPDESK_METRICS_TOKEN_FILE", "")
            if key_path:
                from pathlib import Path
                expected = Path(key_path).read_text().strip()
                if expected and hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + expected):
                    return await self.app(scope, receive, send)
        try:
            # Login also needs Origin; JSON requests with this constraint cannot
            # be initiated by cross-site forms or credentialed CORS requests.
            if method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") != auth.origin:
                raise HTTPException(403, "请求来源无效")
            if path == "/auth/login":
                return await self.app(scope, receive, send)
            if path == "/auth/token":
                raise HTTPException(403, "员工入口不接受共享密钥签发身份")
            token = request.cookies.get(auth.cookie, "")
            actor = await auth.session(token, touch=method not in {"GET", "HEAD", "OPTIONS"})
            expected_identity = request.headers.get("x-employee-session")
            if expected_identity and expected_identity != f"{actor.actor_id}:{actor.auth_version}":
                raise HTTPException(401, "员工账号已切换，请重新验证会话")
            if method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf(token)):
                raise HTTPException(403, "会话校验失败，请刷新后重试")
            needed = role_for(path, method)
            if needed != "employee":
                actor.require_role(needed)
            scope["actor"], scope["staff_session"] = actor, True
            # Raw conversation / turn access is ownership checked, never claimed.
            match = re.fullmatch(r"/conversations/([^/]+)(?:/turns)?", path)
            if match:
                await auth.conversation(actor, match[1])
            match = re.fullmatch(r"/turns/([^/]+)(?:/events)?", path)
            if match:
                conv = await auth.db.fetchval("SELECT conv_id FROM turn_events WHERE turn_id=$1 LIMIT 1", match[1])
                await auth.conversation(actor, conv or "")
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, send)
        reset = current_actor.set(actor)
        auth_reset = current_auth.set(auth)
        started, ended, checked_at = False, False, 0.0

        async def checked_send(message):
            nonlocal started, ended, checked_at
            if ended:
                return
            # The session is rechecked when the response starts and then at most every
            # SESSION_RECHECK_S while a stream is open, not once per SSE chunk: each check
            # is a database query on the shared pool. A failed check (revoked, expired, or
            # the session store unavailable) closes the stream deliberately.
            due = message["type"] == "http.response.start" or (
                message["type"] == "http.response.body" and time.monotonic() - checked_at >= SESSION_RECHECK_S)
            if due and not scope.get("logout_completed"):
                try:
                    live = await auth.session(token)
                    if live.auth_version != actor.auth_version:
                        raise HTTPException(401)
                    checked_at = time.monotonic()
                except Exception:
                    if not started:
                        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
                    await send({"type": "http.response.body", "body": b"" if started else b'{"detail":"Session revoked"}', "more_body": False})
                    ended = True
                    return
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)
        try:
            await self.app(scope, receive, checked_send)
        finally:
            current_actor.reset(reset)
            current_auth.reset(auth_reset)


async def authorize_admin(conn):
    """Recheck the live initiating administrator inside the mutation transaction.

    No context denotes trusted startup/maintenance code. HTTP requests and
    background tasks spawned from them inherit both context variables.
    """
    auth = current_auth.get()
    if auth is not None:
        await auth.administrator(current_actor.get(), conn)


@asynccontextmanager
async def admin_write():
    auth = current_auth.get()
    if auth is None:
        yield
    else:
        async with auth.db.transaction() as conn:
            await authorize_admin(conn)
            yield conn


async def authorize_operations(conn):
    auth, actor = current_auth.get(), current_actor.get()
    if auth is not None:
        if actor is None:
            raise HTTPException(401, "Verified employee required")
        live = await auth.provider(actor.actor_id, actor.instance, conn=conn)
        if live is None or live.auth_version != actor.auth_version:
            raise HTTPException(401, "Employee authorization changed")
        live.require_role("operations")
