"""
数据浏览接口：目录（有哪些业务数据）+ 只读分页预览 + 表概况（只聚合的统计）。

与 `/query` 用**同一套身份解析**（API Key + 可选的 user_id，交集策略照旧）。
这组接口没有任何一个参数能携带 SQL：预览与概况的请求体里只有页码、搜索词、列名与
筛选值，列名要过服务端清单，值走绑定参数——见 `ai_sql_agent.catalog`。

限流单独计数（`catalog_rate_limit_per_minute`，且不超过这把 Key 自己的限速），
不写审计表：审计表同时是 `/history`、每日配额与降级重放的数据源，把「翻了一页表」
记进去，会让用户的提问历史里混进不是提问的东西，还会吃掉他当天的提问额度。
访问痕迹记在结构化日志里（`catalog.preview` / `catalog.profile`，带调用方、表名与 trace_id）。

`/whoami` 不属于数据浏览：它只回答「这次请求是谁、这台服务支持什么」，
关掉数据浏览、或者浏览被限流时照样可用——页面发问之前必须知道自己是谁。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ai_sql_agent import __version__
from ai_sql_agent.auth import ApiKeyRecord, Principal
from ai_sql_agent.catalog import CatalogError, PreviewFilter, PreviewQuery
from ai_sql_agent.catalog.preview import (
    MAX_CELL_CHARS,
    MAX_FILTERS,
    MAX_SEARCH_COLUMNS,
    MAX_SEARCH_LENGTH,
    MAX_VALUE_LENGTH,
    OPERATORS,
)
from ai_sql_agent.catalog.profile import MAX_FACET_VALUES, MAX_FACETS, MAX_TIMELINES
from ai_sql_agent.core import get_logger, get_settings, log_event
from ai_sql_agent.core.metrics import LIMIT_REJECTIONS
from ai_sql_agent.execution.data_time import now_rfc3339

from .security import require_api_key

_logger = get_logger("api.catalog")

router = APIRouter()


class PreviewFilterModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=128)
    op: Literal[OPERATORS]  # type: ignore[valid-type]
    value: Union[str, int, float, None] = ""


class PreviewSortModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"] = "asc"


class PreviewRequest(BaseModel):
    """
    一页预览。**没有 SQL 字段，也不会有**：能表达的只有「哪几行、怎么排」。

    多余的字段直接 422（`extra="forbid"`），不是悄悄忽略——有人往这里塞
    `sql` 时，应该得到一个明确的「这个接口不收这个」，而不是一次看似成功的调用。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = ""
    """与 `/query` 的 user_id 同义：允许 impersonation 的 Key 用它指定最终用户。"""

    page: int = Field(default=1, ge=1, le=1_000_000)
    page_size: int = Field(default=25, ge=1, le=500)
    search: str = Field(default="", max_length=MAX_SEARCH_LENGTH)
    filters: List[PreviewFilterModel] = Field(default_factory=list, max_length=MAX_FILTERS)
    sort: Optional[PreviewSortModel] = None


class ProfileRequest(BaseModel):
    """
    一张表的概况。和预览一样只收「看哪些行」：搜索词与筛选条件，同义、同上限。
    没有页码和排序——聚合结果与它们无关。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = ""
    search: str = Field(default="", max_length=MAX_SEARCH_LENGTH)
    filters: List[PreviewFilterModel] = Field(default_factory=list, max_length=MAX_FILTERS)

    breakdowns: bool = True
    """
    要不要逐字段的取值分布与时间轴。目录页只要每张表的行数时传 false：
    那样只跑一条统计语句，不为每个字段各扫一遍表。
    """


def _principal(request: Request, api_key: Optional[ApiKeyRecord], user_id: str) -> Principal:
    return request.app.state.auth.resolve_principal(api_key, user_id)


def _admit(request: Request, principal: Principal) -> None:
    """开关 + 限流。配额不查——那是按模型用量算的，这里一个 Token 都不花。"""
    settings = get_settings()
    if not settings.catalog_enabled:
        raise HTTPException(
            status_code=404, detail={"code": "disabled", "message": "数据浏览未启用。"}
        )

    # 浏览有自己的桶（翻表不该吃掉提问的额度），但上限不超过这把 Key 自己的限速：
    # 运维把某把 Key 压到每分钟 2 次，说的是「它对这台服务的压力」，不只是提问
    limit = settings.catalog_rate_limit_per_minute
    own_limit = principal.rate_limit_per_minute or settings.default_rate_limit_per_minute
    if own_limit > 0:
        limit = min(limit, own_limit)
    retry_after = request.app.state.limiter.rate_limiter.acquire(
        f"{principal.caller}#catalog", limit
    )
    if retry_after is not None:
        LIMIT_REJECTIONS.inc(reason="catalog_rate_limit")
        log_event(
            _logger,
            logging.WARNING,
            "limit.catalog_rate_limited",
            caller=principal.caller,
            user_id=principal.user_id,
            limit_per_minute=limit,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": f"浏览过于频繁（限制 {limit} 次/分钟），请稍后重试。",
            },
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def _http_error(error: CatalogError) -> HTTPException:
    return HTTPException(
        status_code=error.status, detail={"code": error.code, "message": error.message}
    )


def _identity(principal: Principal) -> Dict[str, Any]:
    return {
        "user_id": principal.user_id,
        "caller": principal.caller,
        "is_admin": principal.is_admin,
        "can_impersonate": principal.can_impersonate,
        "anonymous": principal.anonymous,
    }


def _service() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "feedback_enabled": settings.feedback_enabled,
        "streaming": settings.a2a_streaming,
        "catalog_enabled": settings.catalog_enabled,
        "preview": {
            "max_page_size": settings.catalog_preview_max_page_size,
            "max_offset": settings.catalog_preview_max_offset,
            "max_filters": MAX_FILTERS,
            "max_value_length": MAX_VALUE_LENGTH,
            "max_cell_chars": MAX_CELL_CHARS,
            "max_search_columns": MAX_SEARCH_COLUMNS,
            "operators": list(OPERATORS),
        },
        # 页面据此决定画不画统计。没有这一项的是不带概况接口的旧版本进程
        "profile": {
            "enabled": settings.catalog_profile_enabled,
            "max_facets": MAX_FACETS,
            "max_facet_values": MAX_FACET_VALUES,
            "max_timelines": MAX_TIMELINES,
        },
    }


@router.get("/whoami")
def whoami(
    request: Request,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    本次请求解析出的身份，以及页面要用到的服务能力。

    `/query` 要求请求体里带 user_id，而绑定了用户的 Key 只接受它自己的那个 user_id。
    页面不该让员工去记这个值——从这里取。身份解析与 `/query` 是同一段代码，
    所以这里说你是谁，`/query` 就认你是谁。不查库、不调模型、不受数据浏览开关影响。
    """
    principal = _principal(request, api_key, user_id)
    return {"identity": _identity(principal), "service": _service()}


@router.get("/catalog")
def catalog(
    request: Request,
    user_id: str = "",
    refresh: bool = False,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    当前身份能看到的业务数据目录：数据域 → 表 → 字段，带业务说明与单位。

    内容来自语义层与业务库真实表结构，按表列权限裁剪。库连不上时那个域带
    `status=unavailable`，不返回空清单。`refresh=true` 跳过表结构的短缓存
    （几秒内刚读过的不重读）。

    顺带返回本次请求解析出的身份与页面要用的服务能力（与 `/whoami` 相同），
    页面打开时一次请求就够。
    """
    principal = _principal(request, api_key, user_id)
    _admit(request, principal)

    payload = request.app.state.catalog.describe(principal.policy, refresh=refresh)
    payload["generated_at"] = now_rfc3339()
    payload["identity"] = _identity(principal)
    payload["service"] = _service()

    log_event(
        _logger,
        logging.INFO,
        "catalog.listed",
        caller=principal.caller,
        user_id=principal.user_id,
        domains=len(payload["domains"]),
        access_limited=payload["access_limited"],
    )
    return payload


@router.get("/catalog/{database}/{table}")
def catalog_table(
    database: str,
    table: str,
    request: Request,
    user_id: str = "",
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """单张表的字段说明。表很多的部署里 `/catalog` 不带字段，按表从这里取。"""
    principal = _principal(request, api_key, user_id)
    _admit(request, principal)
    try:
        return request.app.state.catalog.describe_table(
            principal.policy, database=database, table=table
        )
    except CatalogError as error:
        raise _http_error(error) from error


@router.post("/catalog/{database}/{table}/rows")
def catalog_rows(
    database: str,
    table: str,
    body: PreviewRequest,
    request: Request,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    只读预览一页真实数据，支持搜索、按列筛选、排序与翻页。

    走问答同一个执行器：表列权限在执行器入口再判一次，只读、行数上限与超时
    同样生效。返回里的 `as_of` 是**读库的时刻**，不是业务数据的日期；
    业务日期看 `date_ranges`（这批记录里日期列的最早与最晚值）。
    """
    principal = _principal(request, api_key, body.user_id)
    _admit(request, principal)

    query = PreviewQuery(
        page=body.page,
        page_size=body.page_size,
        search=body.search,
        filters=[
            PreviewFilter(column=item.column, op=item.op, value=item.value)
            for item in body.filters
        ],
        sort_column=body.sort.column if body.sort else "",
        sort_descending=bool(body.sort and body.sort.direction == "desc"),
    )
    try:
        return request.app.state.catalog.preview(
            principal.policy,
            database=database,
            table=table,
            query=query,
            caller=principal.caller,
            user_id=principal.user_id,
        )
    except CatalogError as error:
        raise _http_error(error) from error


@router.post("/catalog/{database}/{table}/profile")
def catalog_profile(
    database: str,
    table: str,
    body: ProfileRequest,
    request: Request,
    api_key: Optional[ApiKeyRecord] = Depends(require_api_key),
) -> Dict[str, Any]:
    """
    一张表的概况：总行数、各字段填了多少、取值分布、按月的记录数。

    **只有聚合结果，没有任何一行记录。** 能统计的字段就是预览里看得到的那些，
    筛选条件与预览同义，走同一个执行器（授权关卡、只读校验、超时照旧）。
    一个字段的分布不受它自己的筛选条件影响（`own_filters_excluded`），
    所以页面上点了一个取值之后还能换成另一个。算不出来的分布记在 `skipped` 里。
    """
    principal = _principal(request, api_key, body.user_id)
    _admit(request, principal)
    if not get_settings().catalog_profile_enabled:
        raise HTTPException(
            status_code=404,
            detail={"code": "profile_disabled", "message": "表概况未启用。"},
        )

    query = PreviewQuery(
        search=body.search,
        filters=[
            PreviewFilter(column=item.column, op=item.op, value=item.value)
            for item in body.filters
        ],
    )
    try:
        return request.app.state.catalog.profile(
            principal.policy,
            database=database,
            table=table,
            query=query,
            breakdowns=body.breakdowns,
            caller=principal.caller,
            user_id=principal.user_id,
        )
    except CatalogError as error:
        raise _http_error(error) from error
