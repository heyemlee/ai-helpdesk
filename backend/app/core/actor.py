"""Trusted employee boundary. Stage 5 installs authentication upstream of this dependency.

Only server middleware may put an ActorContext in ASGI scope. Neither user_id nor
X-API-Key supplies employee claims. Tests override this dependency explicitly.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    roles: frozenset[str]
    instance: str
    auth_version: int = 0

    def require_employee(self) -> None:
        if not self.actor_id or not self.instance or not self.roles.intersection({"employee", "operations", "admin"}):
            raise HTTPException(403, {"code": "employee_required", "message": "需要已验证的员工身份"})

    def require_role(self, *roles: str) -> None:
        self.require_employee()
        if "admin" not in self.roles and not self.roles.intersection(roles):
            raise HTTPException(403, {"code": "role_required", "message": "当前员工角色没有此操作权限"})

    def require_policy(self, scope) -> None:
        data = scope.model_dump() if hasattr(scope, "model_dump") else (scope or {})
        audience = data.get("audience", "")
        allowed = {"", "customer", "staff", "employee"}
        if self.roles.intersection({"operations", "admin"}):
            allowed.add("operations")
        if "admin" in self.roles:
            allowed.add("admin")
        if audience not in allowed:
            raise HTTPException(403, "当前角色无权访问该政策受众")

    def readable_audiences(self):
        if "admin" in self.roles:
            return None
        # "*" is the default audience and means "applies to everyone" (policy_scope.eligible).
        return ["", "*", "customer", "staff", "employee"] + (["operations"] if "operations" in self.roles else [])

    def snapshot(self) -> dict:
        return {"actor_id": self.actor_id, "instance": self.instance, "auth_version": self.auth_version}


def get_current_actor(request: Request) -> ActorContext:
    actor = request.scope.get("actor")
    if not isinstance(actor, ActorContext):
        raise HTTPException(401, {"code": "employee_identity_required", "message": "员工登录尚未接入或会话已失效；共享服务密钥不能代替员工身份"})
    actor.require_employee()
    return actor


class ActorDirectory:
    """Live account lookup shared by requests and durable work (stage 5).

    Install a provider backed by the identity system, in every API/worker process.
    Never reconstruct permissions from a queued roles snapshot. No provider fails
    closed, including when a request happens to have an ActorContext in scope.
    """

    def __init__(self, provider: Callable[..., Awaitable[ActorContext | None]] | None = None):
        self.provider = provider

    async def resolve(self, actor_id: str, instance: str, *, conn=None) -> ActorContext:
        # Providers accept conn=None and use the supplied transaction connection
        # when present. Acquiring a second connection inside every locked Case
        # transaction can exhaust the pool and block worker heartbeats.
        actor = await self.provider(actor_id, instance, conn=conn) if self.provider else None
        if not isinstance(actor, ActorContext):
            raise HTTPException(401, {"code": "employee_account_unavailable", "message": "员工登录已失效或账号服务尚未接入"})
        actor.require_employee()
        if actor.actor_id != actor_id or actor.instance != instance:
            raise HTTPException(403, "Employee identity mismatch")
        return actor

    async def refresh(self, actor: ActorContext, *, conn=None) -> ActorContext:
        current = await self.resolve(actor.actor_id, actor.instance, conn=conn)
        if current.auth_version != actor.auth_version:
            raise HTTPException(401, "员工授权已变化，请重新登录")
        return current

    async def from_snapshot(self, snapshot: dict, *, conn=None) -> ActorContext:
        current = await self.resolve(snapshot.get("actor_id", ""), snapshot.get("instance", ""), conn=conn)
        if current.auth_version != snapshot.get("auth_version", 0):
            raise HTTPException(401, "提交时的员工授权已失效")
        return current
