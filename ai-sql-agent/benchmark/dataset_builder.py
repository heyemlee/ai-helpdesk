from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

"""
150 条评测集生成器。

六类各 25 条，覆盖文档 §9.1 定义的查询类型：

- single_table   单表过滤 + 投影
- multi_join     同库多表关联
- aggregation    分组统计
- having         聚合后过滤（多步问题的典型）
- cross_db       跨库，需应用层分步合并
- time_range     相对/绝对时间区间

每条带 gold_columns，用于测「字段召回率」——gold 字段是否全部出现在
检索 top-k 中。这是与执行准确率相互独立的指标：召回不到字段，后面
再准的生成也没用。

gold_sql 统一使用 ATTACH 别名（erp. / pm. / mes. / wms.），
由 benchmark.runner 的 GoldExecutor 在四库 ATTACH 连接上执行。
"""


def _single_table_items() -> List[Dict]:
    items: List[Dict] = []

    for threshold in [0.7, 0.8, 0.9]:
        items.append({
            "category": "single_table",
            "query": f"查询准时率低于{threshold}的供应商名称",
            "gold_sql": f"SELECT supplier_name FROM erp.supplier WHERE on_time_rate < {threshold}",
            "gold_columns": ["erp_db.supplier.supplier_name", "erp_db.supplier.on_time_rate"],
        })

    for status in ["ordered", "partial", "received"]:
        items.append({
            "category": "single_table",
            "query": f"查询采购状态为{status}的采购单号",
            "gold_sql": f"SELECT po_no FROM erp.purchase_order WHERE po_status = '{status}'",
            "gold_columns": ["erp_db.purchase_order.po_no", "erp_db.purchase_order.po_status"],
        })

    for status in ["signed", "in_production", "delivered"]:
        items.append({
            "category": "single_table",
            "query": f"查询订单状态为{status}的客户名称",
            "gold_sql": f"SELECT customer_name FROM erp.sales_order WHERE order_status = '{status}'",
            "gold_columns": ["erp_db.sales_order.customer_name", "erp_db.sales_order.order_status"],
        })

    items.append({
        "category": "single_table",
        "query": "查询实际成本超过预算的项目名称",
        "gold_sql": "SELECT project_name FROM pm.project WHERE actual_cost > budget_amt",
        "gold_columns": ["pm_db.project.project_name", "pm_db.project.actual_cost", "pm_db.project.budget_amt"],
    })
    for unit in ["建筑装修", "消防"]:
        items.append({
            "category": "single_table",
            "query": f"查询业务板块为{unit}的项目名称",
            "gold_sql": f"SELECT project_name FROM pm.project WHERE business_unit = '{unit}'",
            "gold_columns": ["pm_db.project.project_name", "pm_db.project.business_unit"],
        })
    for threshold in [100000, 1000000]:
        items.append({
            "category": "single_table",
            "query": f"查询合同额大于{threshold}的项目名称",
            "gold_sql": f"SELECT project_name FROM pm.project WHERE contract_amt > {threshold}",
            "gold_columns": ["pm_db.project.project_name", "pm_db.project.contract_amt"],
        })

    for process in ["开料", "封边", "排孔", "组装"]:
        items.append({
            "category": "single_table",
            "query": f"查询当前工序为{process}的工单号",
            "gold_sql": f"SELECT work_order_id FROM mes.work_order WHERE current_process = '{process}'",
            "gold_columns": ["mes_db.work_order.work_order_id", "mes_db.work_order.current_process"],
        })
    for threshold in [0.8, 0.95]:
        items.append({
            "category": "single_table",
            "query": f"查询良率低于{threshold}的工单号",
            "gold_sql": f"SELECT work_order_id FROM mes.quality_check WHERE yield_rate < {threshold}",
            "gold_columns": ["mes_db.quality_check.work_order_id", "mes_db.quality_check.yield_rate"],
        })

    for category in ["板材", "门板", "五金"]:
        items.append({
            "category": "single_table",
            "query": f"查询品类为{category}的物料名称",
            "gold_sql": f"SELECT material_name FROM wms.material WHERE category = '{category}'",
            "gold_columns": ["wms_db.material.material_name", "wms_db.material.category"],
        })
    for threshold in [100, 500]:
        items.append({
            "category": "single_table",
            "query": f"查询安全库存大于{threshold}的物料名称",
            "gold_sql": f"SELECT material_name FROM wms.material WHERE safety_stock > {threshold}",
            "gold_columns": ["wms_db.material.material_name", "wms_db.material.safety_stock"],
        })
    return items


def _multi_join_items() -> List[Dict]:
    items: List[Dict] = []

    for threshold in [0.7, 0.8, 0.9]:
        items.append({
            "category": "multi_join",
            "query": f"查询准时率低于{threshold}的供应商的采购单号",
            "gold_sql": (
                "SELECT p.po_no FROM erp.purchase_order p "
                "JOIN erp.supplier s ON p.supplier_id = s.supplier_id "
                f"WHERE s.on_time_rate < {threshold}"
            ),
            "gold_columns": ["erp_db.purchase_order.po_no", "erp_db.purchase_order.supplier_id",
                             "erp_db.supplier.supplier_id", "erp_db.supplier.on_time_rate"],
        })
    for category in ["板材", "五金", "台面", "涂料"]:
        items.append({
            "category": "multi_join",
            "query": f"查询{category}类供应商的采购金额",
            "gold_sql": (
                "SELECT p.po_no, p.po_amt FROM erp.purchase_order p "
                "JOIN erp.supplier s ON p.supplier_id = s.supplier_id "
                f"WHERE s.category = '{category}'"
            ),
            "gold_columns": ["erp_db.purchase_order.po_amt", "erp_db.supplier.category"],
        })
    for status in ["paid", "partial"]:
        items.append({
            "category": "multi_join",
            "query": f"查询付款状态为{status}的采购单的工程号",
            "gold_sql": (
                "SELECT p.job_no FROM erp.purchase_order p "
                "JOIN erp.payment y ON p.po_no = y.po_no "
                f"WHERE y.pay_status = '{status}'"
            ),
            "gold_columns": ["erp_db.purchase_order.job_no", "erp_db.payment.pay_status"],
        })

    for status in ["delayed", "on_time", "pending"]:
        items.append({
            "category": "multi_join",
            "query": f"查询里程碑状态为{status}的项目名称",
            "gold_sql": (
                "SELECT DISTINCT p.project_name FROM pm.project p "
                "JOIN pm.milestone m ON p.project_code = m.project_code "
                f"WHERE m.ms_status = '{status}'"
            ),
            "gold_columns": ["pm_db.project.project_name", "pm_db.milestone.ms_status"],
        })
    for threshold in [10000, 50000, 100000]:
        items.append({
            "category": "multi_join",
            "query": f"查询变更金额大于{threshold}的项目名称",
            "gold_sql": (
                "SELECT DISTINCT p.project_name FROM pm.project p "
                "JOIN pm.change_order c ON p.project_code = c.project_code "
                f"WHERE c.change_amt > {threshold}"
            ),
            "gold_columns": ["pm_db.project.project_name", "pm_db.change_order.change_amt"],
        })
    items.append({
        "category": "multi_join",
        "query": "查询验收结果为passed的项目名称",
        "gold_sql": (
            "SELECT p.project_name FROM pm.project p "
            "JOIN pm.acceptance a ON p.project_code = a.project_code "
            "WHERE a.accept_result = 'passed'"
        ),
        "gold_columns": ["pm_db.project.project_name", "pm_db.acceptance.accept_result"],
    })

    for workshop in ["一车间", "二车间"]:
        items.append({
            "category": "multi_join",
            "query": f"查询{workshop}工单的工时记录中的工人姓名",
            "gold_sql": (
                "SELECT DISTINCT l.worker_name FROM mes.labor_hour l "
                "JOIN mes.work_order w ON l.work_order_id = w.work_order_id "
                f"WHERE w.workshop = '{workshop}'"
            ),
            "gold_columns": ["mes_db.labor_hour.worker_name", "mes_db.work_order.workshop"],
        })
    for threshold in [0.8, 0.95]:
        items.append({
            "category": "multi_join",
            "query": f"查询良率低于{threshold}的工单所属车间",
            "gold_sql": (
                "SELECT w.workshop FROM mes.work_order w "
                "JOIN mes.quality_check q ON w.work_order_id = q.work_order_id "
                f"WHERE q.yield_rate < {threshold}"
            ),
            "gold_columns": ["mes_db.work_order.workshop", "mes_db.quality_check.yield_rate"],
        })

    items.append({
        "category": "multi_join",
        "query": "查询库存低于安全库存的物料名称",
        "gold_sql": (
            "SELECT m.material_name FROM wms.material m "
            "JOIN wms.stock_snapshot s ON m.material_code = s.material_code "
            "WHERE s.on_hand_qty < m.safety_stock"
        ),
        "gold_columns": ["wms_db.material.material_name", "wms_db.material.safety_stock",
                         "wms_db.stock_snapshot.on_hand_qty"],
    })
    for threshold in [200, 500]:
        items.append({
            "category": "multi_join",
            "query": f"查询单位成本大于{threshold}的批次对应的物料名称",
            "gold_sql": (
                "SELECT DISTINCT m.material_name FROM wms.material m "
                "JOIN wms.batch b ON m.material_code = b.material_code "
                f"WHERE b.unit_cost > {threshold}"
            ),
            "gold_columns": ["wms_db.material.material_name", "wms_db.batch.unit_cost"],
        })
    for category in ["板材", "门板"]:
        items.append({
            "category": "multi_join",
            "query": f"查询{category}类物料的在库数量",
            "gold_sql": (
                "SELECT m.material_name, s.on_hand_qty FROM wms.material m "
                "JOIN wms.stock_snapshot s ON m.material_code = s.material_code "
                f"WHERE m.category = '{category}'"
            ),
            "gold_columns": ["wms_db.material.category", "wms_db.stock_snapshot.on_hand_qty"],
        })
    return items


def _aggregation_items() -> List[Dict]:
    items: List[Dict] = []

    items.append({
        "category": "aggregation",
        "query": "统计每个工程的采购总金额",
        "gold_sql": "SELECT job_no, SUM(po_amt) FROM erp.purchase_order GROUP BY job_no",
        "gold_columns": ["erp_db.purchase_order.job_no", "erp_db.purchase_order.po_amt"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计采购单总数",
        "gold_sql": "SELECT COUNT(*) FROM erp.purchase_order",
        "gold_columns": ["erp_db.purchase_order.po_no"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计所有供应商的平均准时率",
        "gold_sql": "SELECT AVG(on_time_rate) FROM erp.supplier",
        "gold_columns": ["erp_db.supplier.on_time_rate"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个品类的供应商数量",
        "gold_sql": "SELECT category, COUNT(*) FROM erp.supplier GROUP BY category",
        "gold_columns": ["erp_db.supplier.category"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计订单总金额",
        "gold_sql": "SELECT SUM(order_amt) FROM erp.sales_order",
        "gold_columns": ["erp_db.sales_order.order_amt"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个订单状态的订单数量",
        "gold_sql": "SELECT order_status, COUNT(*) FROM erp.sales_order GROUP BY order_status",
        "gold_columns": ["erp_db.sales_order.order_status"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计已付款总金额",
        "gold_sql": "SELECT SUM(paid_amt) FROM erp.payment",
        "gold_columns": ["erp_db.payment.paid_amt"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个供应商的采购单数量",
        "gold_sql": "SELECT supplier_id, COUNT(*) FROM erp.purchase_order GROUP BY supplier_id",
        "gold_columns": ["erp_db.purchase_order.supplier_id"],
    })

    items.append({
        "category": "aggregation",
        "query": "统计每个业务板块的项目数量",
        "gold_sql": "SELECT business_unit, COUNT(*) FROM pm.project GROUP BY business_unit",
        "gold_columns": ["pm_db.project.business_unit"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个业务板块的合同额合计",
        "gold_sql": "SELECT business_unit, SUM(contract_amt) FROM pm.project GROUP BY business_unit",
        "gold_columns": ["pm_db.project.business_unit", "pm_db.project.contract_amt"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计所有项目的平均预算金额",
        "gold_sql": "SELECT AVG(budget_amt) FROM pm.project",
        "gold_columns": ["pm_db.project.budget_amt"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计所有项目的实际成本合计",
        "gold_sql": "SELECT SUM(actual_cost) FROM pm.project",
        "gold_columns": ["pm_db.project.actual_cost"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个项目的变更总金额",
        "gold_sql": "SELECT project_code, SUM(change_amt) FROM pm.change_order GROUP BY project_code",
        "gold_columns": ["pm_db.change_order.project_code", "pm_db.change_order.change_amt"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个里程碑状态的节点数量",
        "gold_sql": "SELECT ms_status, COUNT(*) FROM pm.milestone GROUP BY ms_status",
        "gold_columns": ["pm_db.milestone.ms_status"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计项目总数",
        "gold_sql": "SELECT COUNT(*) FROM pm.project",
        "gold_columns": ["pm_db.project.project_code"],
    })

    items.append({
        "category": "aggregation",
        "query": "统计每个工单的工时合计",
        "gold_sql": "SELECT work_order_id, SUM(hours) FROM mes.labor_hour GROUP BY work_order_id",
        "gold_columns": ["mes_db.labor_hour.work_order_id", "mes_db.labor_hour.hours"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个工人的工时合计",
        "gold_sql": "SELECT worker_name, SUM(hours) FROM mes.labor_hour GROUP BY worker_name",
        "gold_columns": ["mes_db.labor_hour.worker_name", "mes_db.labor_hour.hours"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个车间的工单数量",
        "gold_sql": "SELECT workshop, COUNT(*) FROM mes.work_order GROUP BY workshop",
        "gold_columns": ["mes_db.work_order.workshop"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计所有工单的平均良率",
        "gold_sql": "SELECT AVG(yield_rate) FROM mes.quality_check",
        "gold_columns": ["mes_db.quality_check.yield_rate"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个车间的实际产量合计",
        "gold_sql": "SELECT workshop, SUM(actual_qty) FROM mes.capacity_stat GROUP BY workshop",
        "gold_columns": ["mes_db.capacity_stat.workshop", "mes_db.capacity_stat.actual_qty"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个工序的工时合计",
        "gold_sql": "SELECT process_name, SUM(hours) FROM mes.labor_hour GROUP BY process_name",
        "gold_columns": ["mes_db.labor_hour.process_name", "mes_db.labor_hour.hours"],
    })

    items.append({
        "category": "aggregation",
        "query": "统计每个品类的物料数量",
        "gold_sql": "SELECT category, COUNT(*) FROM wms.material GROUP BY category",
        "gold_columns": ["wms_db.material.category"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计每个物料的入库总数量",
        "gold_sql": "SELECT material_code, SUM(in_qty) FROM wms.batch GROUP BY material_code",
        "gold_columns": ["wms_db.batch.material_code", "wms_db.batch.in_qty"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计所有物料的在库数量合计",
        "gold_sql": "SELECT SUM(on_hand_qty) FROM wms.stock_snapshot",
        "gold_columns": ["wms_db.stock_snapshot.on_hand_qty"],
    })
    items.append({
        "category": "aggregation",
        "query": "统计所有批次的平均单位成本",
        "gold_sql": "SELECT AVG(unit_cost) FROM wms.batch",
        "gold_columns": ["wms_db.batch.unit_cost"],
    })
    return items


def _having_items() -> List[Dict]:
    items: List[Dict] = []

    for threshold in [50000, 100000, 200000, 500000]:
        items.append({
            "category": "having",
            "query": f"查询采购总金额超过{threshold}的工程号",
            "gold_sql": (
                "SELECT job_no FROM erp.purchase_order GROUP BY job_no "
                f"HAVING SUM(po_amt) > {threshold}"
            ),
            "gold_columns": ["erp_db.purchase_order.job_no", "erp_db.purchase_order.po_amt"],
        })
    # 阈值取 0/1/2：本数据集下最多的工程有 3 张采购单，取 3 会得到空 gold 结果，
    # 而空结果的评测项没有区分度——任何返回空的 SQL 都会判定为正确。
    for n in [0, 1, 2]:
        items.append({
            "category": "having",
            "query": f"查询采购单数量超过{n}的工程号",
            "gold_sql": (
                "SELECT job_no FROM erp.purchase_order GROUP BY job_no "
                f"HAVING COUNT(*) > {n}"
            ),
            "gold_columns": ["erp_db.purchase_order.job_no"],
        })
    for n in [1, 2]:
        items.append({
            "category": "having",
            "query": f"查询采购单数量超过{n}的供应商编号",
            "gold_sql": (
                "SELECT supplier_id FROM erp.purchase_order GROUP BY supplier_id "
                f"HAVING COUNT(*) > {n}"
            ),
            "gold_columns": ["erp_db.purchase_order.supplier_id"],
        })

    for threshold in [10000, 50000, 100000]:
        items.append({
            "category": "having",
            "query": f"查询变更总金额超过{threshold}的项目编号",
            "gold_sql": (
                "SELECT project_code FROM pm.change_order GROUP BY project_code "
                f"HAVING SUM(change_amt) > {threshold}"
            ),
            "gold_columns": ["pm_db.change_order.project_code", "pm_db.change_order.change_amt"],
        })
    for n in [1, 2]:
        items.append({
            "category": "having",
            "query": f"查询里程碑数量超过{n}的项目编号",
            "gold_sql": (
                "SELECT project_code FROM pm.milestone GROUP BY project_code "
                f"HAVING COUNT(*) > {n}"
            ),
            "gold_columns": ["pm_db.milestone.project_code"],
        })
    items.append({
        "category": "having",
        "query": "查询合同额合计超过1000000的业务板块",
        "gold_sql": (
            "SELECT business_unit FROM pm.project GROUP BY business_unit "
            "HAVING SUM(contract_amt) > 1000000"
        ),
        "gold_columns": ["pm_db.project.business_unit", "pm_db.project.contract_amt"],
    })

    for threshold in [20, 50, 100]:
        items.append({
            "category": "having",
            "query": f"查询工时合计超过{threshold}小时的工单号",
            "gold_sql": (
                "SELECT work_order_id FROM mes.labor_hour GROUP BY work_order_id "
                f"HAVING SUM(hours) > {threshold}"
            ),
            "gold_columns": ["mes_db.labor_hour.work_order_id", "mes_db.labor_hour.hours"],
        })
    for threshold in [30, 80]:
        items.append({
            "category": "having",
            "query": f"查询工时合计超过{threshold}小时的工人姓名",
            "gold_sql": (
                "SELECT worker_name FROM mes.labor_hour GROUP BY worker_name "
                f"HAVING SUM(hours) > {threshold}"
            ),
            "gold_columns": ["mes_db.labor_hour.worker_name", "mes_db.labor_hour.hours"],
        })
    items.append({
        "category": "having",
        "query": "查询实际产量合计超过500的车间",
        "gold_sql": (
            "SELECT workshop FROM mes.capacity_stat GROUP BY workshop "
            "HAVING SUM(actual_qty) > 500"
        ),
        "gold_columns": ["mes_db.capacity_stat.workshop", "mes_db.capacity_stat.actual_qty"],
    })

    for threshold in [500, 1000, 3000]:
        items.append({
            "category": "having",
            "query": f"查询入库总量超过{threshold}的物料编码",
            "gold_sql": (
                "SELECT material_code FROM wms.batch GROUP BY material_code "
                f"HAVING SUM(in_qty) > {threshold}"
            ),
            "gold_columns": ["wms_db.batch.material_code", "wms_db.batch.in_qty"],
        })
    items.append({
        "category": "having",
        "query": "查询批次数量超过1的物料编码",
        "gold_sql": (
            "SELECT material_code FROM wms.batch GROUP BY material_code HAVING COUNT(*) > 1"
        ),
        "gold_columns": ["wms_db.batch.material_code"],
    })
    return items


def _cross_db_items() -> List[Dict]:
    items: List[Dict] = []

    # pm → erp：项目筛选后到 ERP 归集采购（job_no ↔ project_code 命名约定推断）
    items.append({
        "category": "cross_db",
        "query": "查询实际成本超过预算的项目的采购总金额",
        "gold_sql": (
            "SELECT o.job_no, SUM(o.po_amt) FROM erp.purchase_order o "
            "JOIN pm.project p ON o.job_no = p.project_code "
            "WHERE p.actual_cost > p.budget_amt GROUP BY o.job_no"
        ),
        "gold_columns": ["pm_db.project.actual_cost", "pm_db.project.budget_amt",
                         "pm_db.project.project_code", "erp_db.purchase_order.job_no",
                         "erp_db.purchase_order.po_amt"],
    })
    for unit in ["建筑装修", "消防"]:
        items.append({
            "category": "cross_db",
            "query": f"查询{unit}板块项目的采购总金额",
            "gold_sql": (
                "SELECT SUM(o.po_amt) FROM erp.purchase_order o "
                "JOIN pm.project p ON o.job_no = p.project_code "
                f"WHERE p.business_unit = '{unit}'"
            ),
            "gold_columns": ["pm_db.project.business_unit", "erp_db.purchase_order.po_amt"],
        })
    for status in ["active"]:
        items.append({
            "category": "cross_db",
            "query": f"查询状态为{status}的项目的订单金额",
            "gold_sql": (
                "SELECT s.order_no, s.order_amt FROM erp.sales_order s "
                "JOIN pm.project p ON s.job_no = p.project_code "
                f"WHERE p.project_status = '{status}'"
            ),
            "gold_columns": ["pm_db.project.project_status", "erp_db.sales_order.order_amt"],
        })
    for threshold in [1000000]:
        items.append({
            "category": "cross_db",
            "query": f"查询合同额大于{threshold}的项目的采购单号",
            "gold_sql": (
                "SELECT o.po_no FROM erp.purchase_order o "
                "JOIN pm.project p ON o.job_no = p.project_code "
                f"WHERE p.contract_amt > {threshold}"
            ),
            "gold_columns": ["pm_db.project.contract_amt", "erp_db.purchase_order.po_no"],
        })

    # pm → mes
    for status in ["delayed", "pending"]:
        items.append({
            "category": "cross_db",
            "query": f"查询里程碑状态为{status}的项目的生产工单号",
            "gold_sql": (
                "SELECT DISTINCT w.work_order_id FROM mes.work_order w "
                "JOIN pm.milestone m ON w.job_no = m.project_code "
                f"WHERE m.ms_status = '{status}'"
            ),
            "gold_columns": ["pm_db.milestone.ms_status", "mes_db.work_order.work_order_id"],
        })
    items.append({
        "category": "cross_db",
        "query": "查询超预算项目的当前生产工序",
        "gold_sql": (
            "SELECT w.job_no, w.current_process FROM mes.work_order w "
            "JOIN pm.project p ON w.job_no = p.project_code "
            "WHERE p.actual_cost > p.budget_amt"
        ),
        "gold_columns": ["pm_db.project.actual_cost", "pm_db.project.budget_amt",
                         "mes_db.work_order.current_process"],
    })
    for unit in ["建筑装修"]:
        items.append({
            "category": "cross_db",
            "query": f"查询{unit}板块项目的工单数量",
            "gold_sql": (
                "SELECT COUNT(*) FROM mes.work_order w "
                "JOIN pm.project p ON w.job_no = p.project_code "
                f"WHERE p.business_unit = '{unit}'"
            ),
            "gold_columns": ["pm_db.project.business_unit", "mes_db.work_order.work_order_id"],
        })

    # erp → mes
    for status in ["in_production", "delivered"]:
        items.append({
            "category": "cross_db",
            "query": f"查询订单状态为{status}的工程的生产车间",
            "gold_sql": (
                "SELECT DISTINCT w.workshop FROM mes.work_order w "
                "JOIN erp.sales_order s ON w.job_no = s.job_no "
                f"WHERE s.order_status = '{status}'"
            ),
            "gold_columns": ["erp_db.sales_order.order_status", "mes_db.work_order.workshop"],
        })
    for threshold in [1000000]:
        items.append({
            "category": "cross_db",
            "query": f"查询订单金额大于{threshold}的工程的工单号",
            "gold_sql": (
                "SELECT w.work_order_id FROM mes.work_order w "
                "JOIN erp.sales_order s ON w.job_no = s.job_no "
                f"WHERE s.order_amt > {threshold}"
            ),
            "gold_columns": ["erp_db.sales_order.order_amt", "mes_db.work_order.work_order_id"],
        })

    # erp → wms（供应商维度）
    for category in ["板材", "五金"]:
        items.append({
            "category": "cross_db",
            "query": f"查询{category}类供应商的入库批次号",
            "gold_sql": (
                "SELECT b.batch_no FROM wms.batch b "
                "JOIN erp.supplier s ON b.supplier_id = s.supplier_id "
                f"WHERE s.category = '{category}'"
            ),
            "gold_columns": ["erp_db.supplier.category", "wms_db.batch.batch_no"],
        })
    for threshold in [0.8, 0.9]:
        items.append({
            "category": "cross_db",
            "query": f"查询准时率低于{threshold}的供应商的入库总量",
            "gold_sql": (
                "SELECT SUM(b.in_qty) FROM wms.batch b "
                "JOIN erp.supplier s ON b.supplier_id = s.supplier_id "
                f"WHERE s.on_time_rate < {threshold}"
            ),
            "gold_columns": ["erp_db.supplier.on_time_rate", "wms_db.batch.in_qty"],
        })

    # mes → wms / 三库
    items.append({
        "category": "cross_db",
        "query": "查询已完工工单对应项目的合同额",
        "gold_sql": (
            "SELECT DISTINCT p.project_name, p.contract_amt FROM pm.project p "
            "JOIN mes.work_order w ON p.project_code = w.job_no "
            "WHERE w.work_status = 'finished'"
        ),
        "gold_columns": ["mes_db.work_order.work_status", "pm_db.project.contract_amt"],
    })
    items.append({
        "category": "cross_db",
        "query": "查询良率低于0.95的工单对应的客户名称",
        "gold_sql": (
            "SELECT DISTINCT s.customer_name FROM erp.sales_order s "
            "JOIN mes.work_order w ON s.job_no = w.job_no "
            "JOIN mes.quality_check q ON w.work_order_id = q.work_order_id "
            "WHERE q.yield_rate < 0.95"
        ),
        "gold_columns": ["mes_db.quality_check.yield_rate", "erp_db.sales_order.customer_name"],
    })
    for threshold in [100000, 500000]:
        items.append({
            "category": "cross_db",
            "query": f"查询采购总额超过{threshold}的工程对应的项目名称",
            "gold_sql": (
                "SELECT p.project_name FROM pm.project p WHERE p.project_code IN ("
                "SELECT job_no FROM erp.purchase_order GROUP BY job_no "
                f"HAVING SUM(po_amt) > {threshold})"
            ),
            "gold_columns": ["erp_db.purchase_order.po_amt", "pm_db.project.project_name"],
        })
    items.append({
        "category": "cross_db",
        "query": "查询已验收项目的订单金额",
        "gold_sql": (
            "SELECT s.order_no, s.order_amt FROM erp.sales_order s "
            "JOIN pm.acceptance a ON s.job_no = a.project_code"
        ),
        "gold_columns": ["pm_db.acceptance.project_code", "erp_db.sales_order.order_amt"],
    })
    for workshop in ["一车间", "二车间"]:
        items.append({
            "category": "cross_db",
            "query": f"查询{workshop}生产的工程的客户名称",
            "gold_sql": (
                "SELECT DISTINCT s.customer_name FROM erp.sales_order s "
                "JOIN mes.work_order w ON s.job_no = w.job_no "
                f"WHERE w.workshop = '{workshop}'"
            ),
            "gold_columns": ["mes_db.work_order.workshop", "erp_db.sales_order.customer_name"],
        })
    items.append({
        "category": "cross_db",
        "query": "查询进行中项目的采购单数量",
        "gold_sql": (
            "SELECT COUNT(*) FROM erp.purchase_order o "
            "JOIN pm.project p ON o.job_no = p.project_code "
            "WHERE p.project_status = 'active'"
        ),
        "gold_columns": ["pm_db.project.project_status", "erp_db.purchase_order.po_no"],
    })
    items.append({
        "category": "cross_db",
        "query": "查询变更金额大于50000的项目的生产车间",
        "gold_sql": (
            "SELECT DISTINCT w.workshop FROM mes.work_order w "
            "JOIN pm.change_order c ON w.job_no = c.project_code "
            "WHERE c.change_amt > 50000"
        ),
        "gold_columns": ["pm_db.change_order.change_amt", "mes_db.work_order.workshop"],
    })
    return items


def _time_range_items() -> List[Dict]:
    items: List[Dict] = []

    for start, end in [("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30"),
                       ("2024-02-01", "2024-02-29")]:
        items.append({
            "category": "time_range",
            "query": f"查询{start}到{end}期间签单的订单号",
            "gold_sql": (
                f"SELECT order_no FROM erp.sales_order "
                f"WHERE sign_date >= '{start}' AND sign_date <= '{end}'"
            ),
            "gold_columns": ["erp_db.sales_order.order_no", "erp_db.sales_order.sign_date"],
        })
    for start, end in [("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30")]:
        items.append({
            "category": "time_range",
            "query": f"统计{start}到{end}期间的采购总金额",
            "gold_sql": (
                f"SELECT SUM(po_amt) FROM erp.purchase_order "
                f"WHERE po_date >= '{start}' AND po_date <= '{end}'"
            ),
            "gold_columns": ["erp_db.purchase_order.po_amt", "erp_db.purchase_order.po_date"],
        })
    for date in ["2024-03-01", "2024-04-01"]:
        items.append({
            "category": "time_range",
            "query": f"查询{date}之后付款的付款编号",
            "gold_sql": f"SELECT payment_id FROM erp.payment WHERE paid_date > '{date}'",
            "gold_columns": ["erp_db.payment.payment_id", "erp_db.payment.paid_date"],
        })

    for date in ["2024-02-01", "2024-04-01"]:
        items.append({
            "category": "time_range",
            "query": f"查询{date}之后开工的项目名称",
            "gold_sql": f"SELECT project_name FROM pm.project WHERE start_date > '{date}'",
            "gold_columns": ["pm_db.project.project_name", "pm_db.project.start_date"],
        })
    for date in ["2024-05-30", "2024-06-30"]:
        items.append({
            "category": "time_range",
            "query": f"查询计划完工日期早于{date}的项目名称",
            "gold_sql": f"SELECT project_name FROM pm.project WHERE due_date < '{date}'",
            "gold_columns": ["pm_db.project.project_name", "pm_db.project.due_date"],
        })
    for date in ["2024-03-01", "2024-05-01"]:
        items.append({
            "category": "time_range",
            "query": f"查询{date}之后验收的项目编号",
            "gold_sql": f"SELECT project_code FROM pm.acceptance WHERE accept_date > '{date}'",
            "gold_columns": ["pm_db.acceptance.project_code", "pm_db.acceptance.accept_date"],
        })

    for date in ["2024-03-01", "2024-04-01", "2024-04-20"]:
        items.append({
            "category": "time_range",
            "query": f"查询{date}之后排产的工单号",
            "gold_sql": f"SELECT work_order_id FROM mes.work_order WHERE schedule_date > '{date}'",
            "gold_columns": ["mes_db.work_order.work_order_id", "mes_db.work_order.schedule_date"],
        })
    for date in ["2024-03-15", "2024-04-01"]:
        items.append({
            "category": "time_range",
            "query": f"查询{date}之后完工的工单号",
            "gold_sql": f"SELECT work_order_id FROM mes.work_order WHERE finish_date > '{date}'",
            "gold_columns": ["mes_db.work_order.work_order_id", "mes_db.work_order.finish_date"],
        })
    for start, end in [("2024-04-01", "2024-04-30"), ("2024-02-01", "2024-02-29")]:
        items.append({
            "category": "time_range",
            "query": f"统计{start}到{end}期间的工时合计",
            "gold_sql": (
                f"SELECT SUM(hours) FROM mes.labor_hour "
                f"WHERE work_date >= '{start}' AND work_date <= '{end}'"
            ),
            "gold_columns": ["mes_db.labor_hour.hours", "mes_db.labor_hour.work_date"],
        })

    for date in ["2024-02-01", "2024-04-01"]:
        items.append({
            "category": "time_range",
            "query": f"查询{date}之后入库的批次号",
            "gold_sql": f"SELECT batch_no FROM wms.batch WHERE in_date > '{date}'",
            "gold_columns": ["wms_db.batch.batch_no", "wms_db.batch.in_date"],
        })
    for start, end in [("2024-01-01", "2024-02-28"), ("2024-03-01", "2024-04-30")]:
        items.append({
            "category": "time_range",
            "query": f"统计{start}到{end}期间的入库总量",
            "gold_sql": (
                f"SELECT SUM(in_qty) FROM wms.batch "
                f"WHERE in_date >= '{start}' AND in_date <= '{end}'"
            ),
            "gold_columns": ["wms_db.batch.in_qty", "wms_db.batch.in_date"],
        })
    items.append({
        "category": "time_range",
        "query": "查询2024-04-30的库存快照中在库数量",
        "gold_sql": (
            "SELECT material_code, on_hand_qty FROM wms.stock_snapshot "
            "WHERE snapshot_date = '2024-04-30'"
        ),
        "gold_columns": ["wms_db.stock_snapshot.on_hand_qty", "wms_db.stock_snapshot.snapshot_date"],
    })
    return items


_BUILDERS = (
    ("single_table", _single_table_items),
    ("multi_join", _multi_join_items),
    ("aggregation", _aggregation_items),
    ("having", _having_items),
    ("cross_db", _cross_db_items),
    ("time_range", _time_range_items),
)

TARGET_PER_CATEGORY = 25


def build_dataset() -> List[Dict]:
    """生成完整评测集，六类各 25 条，共 150 条。"""
    items: List[Dict] = []
    for name, builder in _BUILDERS:
        produced = builder()
        if len(produced) != TARGET_PER_CATEGORY:
            raise ValueError(
                f"类别 {name} 生成了 {len(produced)} 条，应为 {TARGET_PER_CATEGORY} 条"
            )
        items.extend(produced)

    for index, item in enumerate(items, start=1):
        item["id"] = f"q{index:03d}"
    return items


def main() -> None:
    items = build_dataset()
    counts: Dict[str, int] = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1

    output_path = Path(__file__).parent / "dataset.json"
    output_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"共生成 {len(items)} 条评测数据 → {output_path}")
    for category, count in counts.items():
        print(f"  {category}: {count}")


if __name__ == "__main__":
    main()
