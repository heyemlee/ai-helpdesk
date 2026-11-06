"""Trusted employee boundary. Stage 5 installs authentication upstream of this dependency.

Only server middleware may put an ActorContext in ASGI scope. Neither user_id nor
X-API-Key supplies employee claims. Tests override this dependency explicitly.
"""
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    roles: frozenset[str]
    instance: str

    def require_employee(self) -> None:
        if not self.actor_id or not self.instance or not self.roles.intersection({"employee", "operations", "admin"}):
            raise HTTPException(403, {"code": "employee_required", "message": "需要已验证的员工身份"})


def get_current_actor(request: Request) -> ActorContext:
    actor = request.scope.get("actor")
    if not isinstance(actor, ActorContext):
        raise HTTPException(401, {"code": "employee_identity_required", "message": "员工登录尚未接入或会话已失效；共享服务密钥不能代替员工身份"})
    actor.require_employee()
    return actor
