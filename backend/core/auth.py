"""
用户身份与限流。

核心问题：user_id 原来由客户端请求体随便填——任何人传 user_id=alice 就能把
Alice 的历史摘要和用户画像注入自己的对话上下文，这是跨用户数据泄露，
不只是工程瑕疵。

解决方式：
  1. UserTokenAuth —— HMAC 签名的用户令牌。服务端用 AI_HELPDESK_AUTH_SECRET
     签发 "user_id.过期时间.签名" 三段式令牌，/chat 从令牌推导 user_id，
     不再信任请求体。未配置 secret 时保持 demo 模式（信任请求体），
     行为与旧版完全一致，但启动日志会给出警告。
  2. RateLimiter —— 每用户每分钟固定窗口限流。优先用 Redis（多实例共享计数），
     Redis 不可用时退化为进程内计数，限流语义降级但不失效。
     /chat 每次调用都是真实 LLM 花费，公网暴露必须限流。

刻意不引入 JWT 库：三段式 HMAC 令牌用标准库即可实现且完全够用，
换成 JWT 是改一个模块的事，不影响调用方。
"""
import asyncio
import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """令牌缺失、过期或签名不合法。"""


class UserTokenAuth:
    """
    HMAC 用户令牌。

    令牌格式：{user_id}.{expires_at}.{signature}
    signature = HMAC-SHA256(secret, f"{user_id}.{expires_at}")[:32]

    secret 为空时处于 demo 模式：verify() 直接放行并返回请求体里的 user_id。
    """

    def __init__(self, secret: str = "", default_ttl_s: int = 86400):
        self._secret = (secret or "").strip()
        self._default_ttl_s = default_ttl_s

    @property
    def enabled(self) -> bool:
        return bool(self._secret)

    def issue(self, user_id: str, ttl_s: int | None = None) -> str:
        """签发令牌。仅在配置了 secret 时可用。"""
        if not self.enabled:
            raise AuthError("未配置 AI_HELPDESK_AUTH_SECRET，无法签发令牌")
        user_id = (user_id or "").strip()
        if not user_id or "." in user_id:
            raise AuthError("user_id 不能为空且不能包含 '.'")
        expires_at = int(time.time()) + (ttl_s or self._default_ttl_s)
        return f"{user_id}.{expires_at}.{self._sign(user_id, expires_at)}"

    def verify(self, token: str | None, claimed_user_id: str | None = None) -> str:
        """
        校验令牌并返回可信的 user_id。

        demo 模式（未配置 secret）：返回 claimed_user_id，与旧行为一致。
        鉴权模式：token 必须合法；若请求体还带了 user_id 且与令牌不符，
        直接拒绝——这是把伪造意图暴露出来，而不是静默用令牌覆盖。
        """
        if not self.enabled:
            return (claimed_user_id or "anonymous").strip() or "anonymous"

        if not token:
            raise AuthError("缺少 X-User-Token")
        parts = token.strip().split(".")
        if len(parts) != 3:
            raise AuthError("令牌格式错误")
        user_id, expires_raw, signature = parts
        try:
            expires_at = int(expires_raw)
        except ValueError as exc:
            raise AuthError("令牌格式错误") from exc
        if not hmac.compare_digest(signature, self._sign(user_id, expires_at)):
            raise AuthError("令牌签名不合法")
        if expires_at < time.time():
            raise AuthError("令牌已过期")
        if claimed_user_id and claimed_user_id.strip() not in ("", "anonymous", user_id):
            raise AuthError("user_id 与令牌不符")
        return user_id

    def _sign(self, user_id: str, expires_at: int) -> str:
        payload = f"{user_id}.{expires_at}".encode()
        return hmac.new(self._secret.encode(), payload, hashlib.sha256).hexdigest()[:32]


class RateLimiter:
    """
    每用户固定窗口限流（默认窗口 60s）。

    优先用 Redis INCR+EXPIRE（原子、多实例共享）；Redis 不可用时退化为
    进程内计数并记录一次警告——单实例语义不变，多实例时每实例各限各的，
    总放行量最多放大为实例数倍，仍然有界。

    limit_per_window <= 0 表示关闭限流（demo 默认）。
    """

    def __init__(self, redis_client=None, limit_per_window: int = 0, window_s: int = 60):
        self._redis = redis_client
        self.limit = int(limit_per_window)
        self.window_s = int(window_s)
        self._local: dict[str, tuple[int, float]] = {}  # key → (count, window_start)
        self._local_lock = asyncio.Lock()
        self._redis_warned = False

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    async def check(self, user_id: str) -> bool:
        """允许本次请求返回 True；超限返回 False。"""
        if not self.enabled:
            return True
        key = f"ratelimit:{user_id}:{int(time.time() // self.window_s)}"
        if self._redis is not None:
            try:
                count = await self._redis.incr(key)
                if count == 1:
                    await self._redis.expire(key, self.window_s * 2)
                return count <= self.limit
            except Exception as exc:
                if not self._redis_warned:
                    self._redis_warned = True
                    logger.warning(f"限流 Redis 不可用，退化为进程内计数: {exc}")
        return await self._check_local(user_id)

    async def _check_local(self, user_id: str) -> bool:
        now = time.time()
        window_start = now - (now % self.window_s)
        async with self._local_lock:
            count, start = self._local.get(user_id, (0, window_start))
            if start < window_start:
                count = 0
            count += 1
            self._local[user_id] = (count, window_start)
            if len(self._local) > 10000:  # 防止 key 无限增长
                self._local = {
                    k: v for k, v in self._local.items() if v[1] >= window_start
                }
            return count <= self.limit
