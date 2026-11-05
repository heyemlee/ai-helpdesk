"""
客服转来的问法（`customer_cases.json`）：客户在客服对话里怎么问订单，就原样拿来问。

为什么单独要这一类：客服问订单时，规则预取把客户原话直接转给 SQL Agent，查回来的数据
被答案守卫当成出处——SQL Agent 查错了单，客服会照着说。150 条评测集全是分析员问法
（「查询…的…」，输出什么写得清清楚楚），客户问法一道也没有；客服那边的评测又都用替身
代替 SQL Agent。两头都没测到这一段。

和 150 条评测集的区别：

- **库不一样。** 题目对着生产在用的精简业务库（`tools/simple_erp`：订单、生产、仓储、
  配送与售后四个库）。这里按固定种子现建一份：生成器是确定的，按单号查到的状态、日期、
  金额、明细和生产那份一致（只有客户名、供应商名的写法不同，所以题目一律按单号问）。
- **路由也看。** 生产入口先判「查库还是对话问答」，判成对话问答的根本不查库。每一题都
  记下生产入口会怎么判（`route`），但流水线照样跑、照样按标准答案打分。
- **不进门禁。** 报告里单独一行（`customer`），不并进单库准确率，`gate.py` 和
  `thresholds.json` 不读它。

判分与评测集相同（`runner.result_matches`）。没有模型凭证时走 Mock，只能验证这一类
能跑通、能单独出一行，分数没有意义。
"""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Optional

from benchmark.runner import (
    CUSTOMER_CATEGORY,
    MISMATCH_LABELS,
    BenchmarkReport,
    BenchmarkRunner,
)

CUSTOMER_CASES = Path(__file__).parent / "customer_cases.json"

NOT_RUN_LABEL = "没查成（执行报错或没生成步骤）"

PGVECTOR_DSN_ENV = "AI_SQL_PGVECTOR_DSN"


@contextmanager
def _without_pgvector() -> Iterator[None]:
    """
    这一轮不碰 pgvector：Schema 向量索引一律用内存的，退出时把原来的连接串放回去。

    pgvector 里的索引表按命名空间全局共用（`tier_databases`、`tier_tables`、
    `columns_<库名>`），指纹一变就整表 TRUNCATE 重建。现建的精简库客户名、供应商名
    和生产那份写法不同，指纹必然不一样：同一个 DSN 上要是跑着 SQL Agent，它内存里的
    「文档序号 → 表/字段」还指着旧文档，检索就会拿到错的表和字段。所以这里不管
    环境变量还是 `.env` 里配了 DSN，都清空（环境变量里的空串盖得过 `.env`）。
    """
    from ai_sql_agent.core import reload_settings

    saved = os.environ.get(PGVECTOR_DSN_ENV)
    os.environ[PGVECTOR_DSN_ENV] = ""
    reload_settings()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(PGVECTOR_DSN_ENV, None)
        else:
            os.environ[PGVECTOR_DSN_ENV] = saved
        reload_settings()


def build_simple_erp(root: Path) -> Path:
    """在 root 下按固定种子现建精简业务库和它的语义层，返回语义层目录。"""
    from tools.sim_erp.build import build as build_source
    from tools.simple_erp.build import build
    from tools.simple_erp.semantic import build_semantic

    build_source(root / "source")
    build(root / "source", root / "simple")
    build_semantic(root / "simple", root / "semantic")
    return root / "semantic"


def production_router():
    """
    和生产入口（`DynamicSqlAgentService`）同一种路由：有模型凭证时模型判、
    模型判不出来退回规则；没有凭证只用规则。模型客户端用同一个构造函数，两边不会走样。
    """
    from ai_sql_agent.pipeline import (
        DynamicIntentRouter,
        DynamicSqlAgentService,
        ModelIntentClassifier,
    )

    client = DynamicSqlAgentService._build_model_client()
    return DynamicIntentRouter(
        model_classifier=ModelIntentClassifier(client) if client else None
    )


@contextmanager
def customer_runner(
    *,
    enable_repair: Optional[bool] = None,
    access_check: Optional[Callable] = None,
) -> Iterator[BenchmarkRunner]:
    """
    现建精简业务库，给出跑 `customer_cases.json` 的评测器；退出时库连同临时目录一起删掉。

    整个过程不碰 pgvector（见 `_without_pgvector`）：建索引和每一题的检索都在内存里。
    """
    from ai_sql_agent.pipeline import Text2SQLPipeline
    from ai_sql_agent.pipeline.objects import PipelineConfig
    from ai_sql_agent.semantic import load_semantic_dir

    with _without_pgvector(), tempfile.TemporaryDirectory(prefix="customer-cases-") as tmp:
        config = PipelineConfig(
            semantic_layer=load_semantic_dir(build_simple_erp(Path(tmp)))
        )
        if enable_repair is not None:
            config.enable_repair = enable_repair
        runner = BenchmarkRunner(
            pipeline=Text2SQLPipeline(config),
            dataset_path=CUSTOMER_CASES,
            access_check=access_check,
            router=production_router(),
        )
        try:
            yield runner
        finally:
            runner.gold_executor.close()


def customer_summary(report: BenchmarkReport) -> List[str]:
    """
    报告里客服问法那一行，和它下面的错因。报告里没有这一类就是空列表。

    「取值或行数不对」的静默错误单独数：客服拿到这种结果会照着说给客户；
    「多输出了列」的取值是对的，客服照着说也不会错。
    """
    items = [item for item in report.items if item.category == CUSTOMER_CATEGORY]
    if not items:
        return []
    correct = sum(1 for item in items if item.correct)
    lines = [
        f"客服转来的问法（{CUSTOMER_CATEGORY}，单独一行，不计入门禁）："
        f"{correct}/{len(items)} = {correct / len(items):.2%}"
    ]

    wrong = [item for item in items if not item.correct]
    if wrong:
        reasons = Counter(item.mismatch for item in wrong)
        parts = [
            f"{MISMATCH_LABELS.get(reason, NOT_RUN_LABEL)} {count} 道"
            for reason, count in reasons.most_common()
        ]
        lines.append(f"  答错 {len(wrong)} 道：" + "，".join(parts))

    silent = [item for item in items if item.silent_error]
    misleading = [item for item in silent if item.mismatch in ("wrong_values", "row_count")]
    lines.append(
        f"  静默答错 {len(silent)} 道，其中取值或行数不对 {len(misleading)} 道"
        + (f"（{'、'.join(item.item_id for item in misleading)}）" if misleading else "")
    )

    routed = [item for item in items if item.route]
    if routed:
        by_model = sum(1 for item in routed if "model_classifier" in item.route_signals)
        if by_model == len(routed):
            source = "模型判"
        elif by_model == 0:
            source = "规则判"
        else:
            source = f"模型判 {by_model} 道，退回规则 {len(routed) - by_model} 道"
        away = [item for item in routed if item.route != "database_query"]
        lines.append(
            f"  生产入口的路由（{source}）：{len(away)} 道会判成对话问答、不查库"
            + (f"（{'、'.join(item.item_id for item in away)}）" if away else "")
        )
    return lines
