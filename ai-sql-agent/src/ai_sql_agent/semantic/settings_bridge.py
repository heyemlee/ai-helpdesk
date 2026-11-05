from __future__ import annotations

import logging

from ai_sql_agent.core import get_logger, get_settings, log_event

from .loader import demo_semantic_layer, load_semantic_dir
from .objects import SemanticLayer

_logger = get_logger("semantic")


def resolve_semantic_layer(semantic_dir: str = "") -> SemanticLayer:
    """
    按配置解析语义层：配了目录就读目录，没配就用内置 Demo 元数据。

    读目录失败**直接抛错，不静默回退到 Demo**：回退会让服务带着四个演示库
    正常起来，用户问真实业务问题得到的是演示数据的答案——比起不起来危险得多。
    """
    resolved = (semantic_dir or get_settings().semantic_dir).strip()
    if not resolved:
        layer = demo_semantic_layer()
        log_event(
            _logger,
            logging.INFO,
            "semantic.loaded",
            source="demo",
            databases=layer.database_names(),
            hint="接真实业务库请配置 AI_SQL_SEMANTIC_DIR，见 docs/deployment.md",
        )
        return layer

    layer = load_semantic_dir(resolved)
    log_event(
        _logger,
        logging.INFO,
        "semantic.loaded",
        source=layer.source,
        databases=layer.database_names(),
        tables=sum(len(db.tables) for db in layer.databases.values()),
        cross_database_links=len(layer.cross_database_links),
        metric_definitions=len(layer.metric_definitions),
    )
    return layer


def active_semantic_layer() -> SemanticLayer:
    """当前配置下生效的语义层。"""
    return resolve_semantic_layer()
