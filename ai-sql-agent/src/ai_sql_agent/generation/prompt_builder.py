from __future__ import annotations

from ai_sql_agent.execution.dialects import get_dialect

from .objects import SqlGenerationRequest


class SqlPromptBuilder:
    """
    SQL 生成 Prompt 构建器。

    注意：
    - Prompt 中不放数据库名称，数据库名称只用于后续执行路由。
    """

    def build(self, request: SqlGenerationRequest) -> str:
        """
        构建 Coder 模型 Prompt。
        """
        cot_step = request.cot_step
        schema_context = request.local_schema.to_prompt_context()
        dialect_rules = get_dialect(request.sql_dialect).to_prompt_rules()
        user_query = getattr(request, "user_query", "")
        query_section = ("\n# 当前用户查询（用于核对业务目标，不是返回值来源：不得用其中的编号替代真实列）\n"
                         + user_query) if user_query else ""

        certified_section = ""
        if request.certified_examples.strip():
            certified_section = f"\n{request.certified_examples.strip()}\n"

        previous_results_section = ""
        if request.previous_results.strip():
            previous_results_section = f"""
# 上一步结果（已在另一个数据库执行完毕）
下面是上一步的结果。**上一步的筛选条件已经生效，本步骤不要重复实现它**，
只需要把结果里的取值当成已知常量用掉。

用法：直接抄下面「可直接用作本步骤的字面量筛选条件」那一行放进 WHERE。
禁止：跨库 JOIN、跨库子查询、在本步骤重新推导上一步的筛选逻辑——
另一个库的表在这里不存在，写了必定报 no such table。

{request.previous_results}
"""

        return f"""你是一个SQL生成助手。

请根据当前步骤的CoT规划和相关Schema信息生成SQL语句。

要求：
1. 只能使用Schema中提供的表、字段和表关联关系。
2. 严格按照CoT中的操作指令生成SQL。
3. 不得编造不存在的表、字段或关联关系。
4. 只输出SQL语句，不输出解释性内容。
5. SQL需要符合下方「方言约束」中指定的语法。
6. 不要输出Markdown代码块，不要输出```sql。
7. **表名前不要加数据库名前缀**。执行时已经连到目标库，写 `pm_db.project`
   会报「no such table」，直接写 `project`。
8. 只输出一条SQL语句，不要输出多条或附加说明。
9. **区分业务目标绑定、明细行标识和内部主键。** 明细查询须返回用户指定的业务实体编号；
   查询该实体的子记录时，同时保留父实体业务编号和区分各条明细的业务字段（例如配件名称）。
   名称不能替代明确指定的业务编号。编号必须来自真实列，不能从问题或 WHERE 常量补造。
   内部主键只作溯源，不是额外的答案字段；未请求时不要附加。汇总、分组、DISTINCT 和
   属性取值查询按原问题粒度执行，不为附加编号或内部主键改变结果粒度。
10. **JOIN 的基数写在「表关联关系」括号里**。沿「一对多」JOIN 之后，
    如果输出列只来自「一」那一侧，必须加 DISTINCT，否则「一」那侧的行会重复。
11. **列名是给调用方看的表头**。输出目标明确指定别名时按指定别名返回，其余直接输出的字段保持原名；
    计算列、聚合列的别名（AS）用英文小写加下划线（如 `total_paid_amt`、
    `unpaid_balance`），不要用中文别名——提问的人不一定读中文。
12. **操作指令没要求汇总，就逐条输出，不要聚合。** 只有操作指令写了汇总、求和、计数、
    平均、最大 / 最小、排名，或者输出目标写的是总额、合计、个数时，才用 SUM / COUNT /
    AVG / MAX / MIN 或 GROUP BY。输出目标是某个字段（如 po_amt、on_hand_qty）时，
    逐条输出这个字段，不要把它加总成一个数。「逐条」不等于不去重：操作指令写了去重，就加 DISTINCT。
13. 只实现操作指令里写到的筛选条件，不要自己补充操作指令没写的条件（例如状态）。
14. 输出「输出目标」列出的全部字段，并保留规则 9 要求的真实业务目标绑定；不要附带无关字段。
    操作指令没写左关联、没写「没有记录的记为0」，就不要用 LEFT JOIN 和 COALESCE 补行。
15. 逐行原始记录必须保留输出目标要求的所有原始列，不得只选多表共有列，
    不得包装进 JSON 字符串或以常量冒充原始列。要求同一行证据时，按 Schema 中的真实关系
    关联并保留各条明细，不得将头和明细 UNION 成互相缺列的记录。
16. 计算的是/否判断用1表示是、0表示否，保留NULL表示无法判断；不要用“是/否”或
    “Yes/No”文本代替结构化布尔值。直接查询的业务状态、日期等原始字段仍保留原值。

# 方言约束
{dialect_rules}
{certified_section}
# 当前步骤CoT
处理对象：{cot_step.processing_objects}
操作指令：{cot_step.operation_instruction}
输出目标：{cot_step.output_target}
{query_section}
{previous_results_section}
# Schema信息
{schema_context}
"""
