"""
语义元数据命令行工具。

    python -m ai_sql_agent.semantic export semantic/          # 导出 Demo 元数据当模板
    python -m ai_sql_agent.semantic validate semantic/        # 校验，并与真实库比对结构
    python -m ai_sql_agent.semantic validate semantic/ --offline   # 只做离线检查（CI 用）
    python -m ai_sql_agent.semantic show                      # 看当前配置生效的是哪一份

validate 返回非 0 表示有错误，可以直接卡在流水线里。
"""

from __future__ import annotations

import argparse
import sys

from .loader import demo_semantic_layer, load_semantic_dir, write_semantic_dir
from .settings_bridge import resolve_semantic_layer
from .validator import coverage_summary, format_summary, validate_semantic_layer


def _export(args: argparse.Namespace) -> int:
    layer = demo_semantic_layer()
    written = write_semantic_dir(
        layer, args.directory, fmt=args.format, overwrite=args.overwrite
    )
    print(f"已导出 {len(written)} 个文件到 {args.directory}/：")
    for path in written:
        print(f"  {path.name}")
    print()
    print(format_summary(coverage_summary(layer)))
    print()
    print(
        "这是四个演示库的元数据，当模板改：把表名字段名换成真实库的，\n"
        "描述和别名按业务口径重写，然后配置 AI_SQL_SEMANTIC_DIR 指向这个目录。"
    )
    return 0


def _validate(args: argparse.Namespace) -> int:
    try:
        layer = load_semantic_dir(args.directory)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"加载失败：{exc}", file=sys.stderr)
        return 2

    print(format_summary(coverage_summary(layer)))
    print()
    report = validate_semantic_layer(layer, live=not args.offline)
    print(report.render())

    if not report.ok:
        print("\n元数据有错误，接上去会让链路答错，请先修。", file=sys.stderr)
        return 1
    return 0


def _show(args: argparse.Namespace) -> int:
    layer = resolve_semantic_layer()
    origin = "内置 Demo 元数据" if layer.is_demo else layer.source
    print(f"当前生效：{origin}")
    print(format_summary(coverage_summary(layer)))
    print(f"  跨库关联键 {len(layer.cross_database_links)} 条")
    print(f"  指标口径消歧 {len(layer.metric_disambiguation)} 条")
    print(f"  业务指标定义 {len(layer.metric_definitions)} 条")
    if layer.is_demo:
        print(
            "\n还在用演示元数据。接真实业务库：\n"
            "  1. python -m ai_sql_agent.semantic export semantic/\n"
            "  2. 按真实库改写 semantic/*.yaml\n"
            "  3. python -m ai_sql_agent.semantic validate semantic/\n"
            "  4. 配置 AI_SQL_SEMANTIC_DIR=semantic/"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ai_sql_agent.semantic", description="业务语义元数据工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="导出内置 Demo 元数据当模板")
    export.add_argument("directory", help="导出目录")
    export.add_argument("--format", choices=("yaml", "json"), default="yaml")
    export.add_argument("--overwrite", action="store_true", help="覆盖已存在的文件")
    export.set_defaults(func=_export)

    validate = sub.add_parser("validate", help="校验元数据，并与真实库比对结构")
    validate.add_argument("directory", help="元数据目录")
    validate.add_argument(
        "--offline",
        action="store_true",
        help="只做离线检查，不连业务库（CI 里没有业务库连接时用）",
    )
    validate.set_defaults(func=_validate)

    show = sub.add_parser("show", help="查看当前配置生效的元数据")
    show.set_defaults(func=_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
