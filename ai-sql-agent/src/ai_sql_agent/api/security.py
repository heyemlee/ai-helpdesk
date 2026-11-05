from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

from ai_sql_agent.auth import ApiKeyRecord, ApiKeyStore, Principal
from ai_sql_agent.core import get_logger, get_settings, log_event

_logger = get_logger("api.auth")

API_KEY_HEADER = "X-API-Key"


class AuthGuard:
    """
    请求鉴权。

    没有网关，身份由服务自己校验：`Authorization: Bearer <key>` 或
    `X-API-Key: <key>`。`AI_SQL_AUTH_MODE=disabled` 时放行并使用匿名身份，
    只用于本地开发——启动时会打一条显式警告。
    """

    def __init__(self, store: Optional[ApiKeyStore] = None):
        settings = get_settings()
        self.mode = settings.auth_mode
        self.store = store

        if self.mode == "api_key":
            self.store = store or ApiKeyStore.from_settings()
            if len(self.store) == 0:
                raise RuntimeError(
                    "AI_SQL_AUTH_MODE=api_key 但没有加载到任何 API Key，"
                    "请配置 AI_SQL_API_KEYS_FILE 或 AI_SQL_API_KEYS。"
                )
            log_event(
                _logger, logging.INFO, "auth.enabled", keys=len(self.store)
            )
        else:
            log_event(
                _logger,
                logging.WARNING,
                "auth.disabled",
                hint="AI_SQL_AUTH_MODE=disabled，接口无鉴权，仅限本地开发",
            )

    @property
    def enabled(self) -> bool:
        return self.mode == "api_key"

    def authenticate(self, request: Request) -> Optional[ApiKeyRecord]:
        """校验请求头里的 API Key；鉴权关闭时返回 None。"""
        if not self.enabled:
            return None

        api_key = self._extract_key(request)
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="缺少 API Key，请在 Authorization: Bearer 或 X-API-Key 中提供。",
                headers={"WWW-Authenticate": "Bearer"},
            )

        record = self.store._match(api_key) if self.store else None
        if record is None:
            log_event(
                _logger,
                logging.WARNING,
                "auth.rejected",
                path=request.url.path,
                client=request.client.host if request.client else "",
            )
            raise HTTPException(status_code=401, detail="API Key 无效。")

        return record

    def resolve_principal(
        self,
        record: Optional[ApiKeyRecord],
        requested_user_id: str,
    ) -> Principal:
        """把 Key 与请求里的 user_id 解析成本次请求的身份。"""
        if record is None:
            return Principal.anonymous_principal(requested_user_id or "anonymous")

        assert self.store is not None
        try:
            return self.store.resolve(record.key, requested_user_id)
        except PermissionError as exc:
            log_event(
                _logger,
                logging.WARNING,
                "auth.impersonation_denied",
                caller=record.caller,
                requested_user_id=requested_user_id,
            )
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @staticmethod
    def _extract_key(request: Request) -> str:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return request.headers.get(API_KEY_HEADER, "").strip()


def get_auth_guard(request: Request) -> AuthGuard:
    return request.app.state.auth


def require_api_key(request: Request) -> Optional[ApiKeyRecord]:
    """FastAPI 依赖：校验 API Key，返回命中的 Key 配置（鉴权关闭时为 None）。"""
    return get_auth_guard(request).authenticate(request)


def require_admin(request: Request) -> Principal:
    """
    FastAPI 依赖：运维接口专用，要求 Key 带 `is_admin`。

    鉴权关闭时（本地开发）匿名身份即管理员。
    """
    guard = get_auth_guard(request)
    record = guard.authenticate(request)
    principal = guard.resolve_principal(record, "")

    if not principal.is_admin:
        log_event(
            _logger,
            logging.WARNING,
            "auth.admin_denied",
            caller=principal.caller,
            path=request.url.path,
        )
        raise HTTPException(status_code=403, detail="该接口需要管理员权限的 API Key。")

    return principal
