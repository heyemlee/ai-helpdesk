"""Server-owned policy applicability. Unknown dimensions match only explicit '*'."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(UTC)


class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["draft", "published", "retired"] = "draft"
    version: str = Field(default="legacy-unreviewed", min_length=1, max_length=80)
    product: str = Field(default="*", min_length=1, max_length=100)
    region: str = Field(default="*", min_length=1, max_length=100)
    audience: str = Field(default="*", min_length=1, max_length=100)
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("version", "product", "region", "audience")
    @classmethod
    def clean(cls, value):
        if value != value.strip() or not value.strip() or "\x00" in value:
            raise ValueError("Policy identifiers must be nonempty, trimmed text")
        return value

    @field_validator("effective_at", "expires_at")
    @classmethod
    def aware(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Policy dates require a timezone")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def dates(self):
        if self.status == "published" and (self.effective_at is None or self.version == "legacy-unreviewed"):
            raise ValueError("Publish requires an explicit version and effective date")
        if self.expires_at and (not self.effective_at or self.expires_at <= self.effective_at):
            raise ValueError("Expiry must be later than the effective date")
        return self


class PolicyScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    product: str = Field(default="", max_length=100)
    region: str = Field(default="", max_length=100)
    audience: str = Field(default="", max_length=100)
    # Only an authenticated employee request may supply this. Tools cannot.
    as_of: datetime | None = None

    @field_validator("as_of")
    @classmethod
    def aware(cls, value):
        return PolicyMetadata.aware(value)

    @field_validator("product", "region", "audience")
    @classmethod
    def exact(cls, value):
        if value == "*" or value != value.strip() or "\x00" in value:
            raise ValueError("Unknown scope is empty, never a wildcard")
        return value

    def at(self):
        return self.as_of or now()


_scope: ContextVar[PolicyScope | None] = ContextVar("policy_scope", default=None)


def current_scope() -> PolicyScope:
    return _scope.get() or PolicyScope()


@contextmanager
def bind_policy_scope(scope: PolicyScope):
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def metadata(value) -> PolicyMetadata:
    return PolicyMetadata.model_validate(json.loads(value) if isinstance(value, str) else value or {})


def eligible(policy: PolicyMetadata, scope: PolicyScope, at=None) -> bool:
    at = at or scope.at()
    return (policy.status == "published" and policy.effective_at is not None and policy.effective_at <= at
            and (policy.expires_at is None or at < policy.expires_at)
            and all(getattr(policy, k) == "*" or (getattr(scope, k) and getattr(policy, k) == getattr(scope, k))
                    for k in ("product", "region", "audience")))


def policy_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def sql_filter(scope: PolicyScope, *, postgres=False, start=1, at=None):
    """The predicate is applied before similarity/lexical ranking and LIMIT."""
    at = at or scope.at()
    mark = (lambda n: f"${n}") if postgres else (lambda n: f"?{n}")
    col = (lambda key: f"d.policy->>'{key}'") if postgres else (lambda key: f"json_extract(d.policy, '$.{key}')")
    timestamp = (lambda expr: f"({expr})::timestamptz") if postgres else (lambda expr: f"julianday({expr})")
    p = mark(start)
    clauses = [f"{col('status')}='published'", f"{timestamp(col('effective_at'))}<={timestamp(p)}",
               f"({col('expires_at')} IS NULL OR {timestamp(col('expires_at'))}>{timestamp(p)})"]
    args = [at if postgres else at.isoformat()]
    for i, key in enumerate(("product", "region", "audience"), start + 1):
        clauses.append(f"({col(key)}='*' OR ({mark(i)}<>'' AND {col(key)}={mark(i)}))")
        args.append(getattr(scope, key))
    return " AND ".join(clauses), args
