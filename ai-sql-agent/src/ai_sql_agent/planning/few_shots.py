"""跨库规划的 Few-shot 范例。

为什么单独放一个范例：跨库拆步的规则此前只以文字指令存在
（「涉及多个数据库需要拆分步骤」「不要跨库 Join」），失败样本显示模型经常
读懂了规则却写不对格式——把两个库压进一步，或者第一步不输出关联键，
导致第二步无从下手。模型跟随格式示例远强于跟随文字描述，
所以给它一个从问题到两步计划、含关联键传递的完整走通例子。

只在检索命中多个库时注入：单库问题看到这个例子会倾向于多拆一步，
既费一次模型调用又多一次出错机会。
"""

from __future__ import annotations

from .objects import FewShotExample


_CROSS_DB_SCHEMA = """数据库：pm_db（项目与工期域）
  project 项目表
    - project_code  项目编号
    - project_name  项目名称
    - budget_amt    预算金额（不含税）
    - actual_cost   实际成本

数据库：erp_db（订单与采购域）
  purchase_order 采购单表
    - po_no    采购单号
    - job_no   工程编号
    - po_amt   采购金额（含税）

跨库说明：pm_db.project.project_code 与 erp_db.purchase_order.job_no 指同一个工程，
但分属不同数据库，没有外键，禁止直接 JOIN，必须拆成两个步骤、用字面量传递。"""


_CROSS_DB_OUTPUT = """步骤1：
(
  数据库: pm_db,
  处理对象: project表（字段：project_code, budget_amt, actual_cost）,
  操作指令: 先在project表中筛选actual_cost大于budget_amt的记录；再输出这些记录的project_code，作为传给下一步的关联键,
  输出目标: project_code,
  预期输出量级: 数条
)

步骤2：
(
  数据库: erp_db,
  处理对象: purchase_order表（字段：job_no, po_amt）,
  操作指令: 先把步骤1输出的project_code取值当作字面量列表，筛选purchase_order中job_no落在该列表内的记录；再对po_amt求和得到采购总金额,
  输出目标: 采购总金额,
  预期输出量级: 单条
)"""


CROSS_DB_EXAMPLE = FewShotExample(
    user_query="查询实际成本超过预算的项目的采购总金额",
    schema_context=_CROSS_DB_SCHEMA,
    output=_CROSS_DB_OUTPUT,
)


def few_shots_for(database_count: int) -> list:
    """按检索命中的库数量决定注入哪些范例。"""
    return [CROSS_DB_EXAMPLE] if database_count > 1 else []
