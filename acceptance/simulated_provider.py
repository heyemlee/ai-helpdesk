"""Explicit external-provider simulation for infrastructure load only.

The application, SQL planner/executor, KB retrieval, image workflow, auth and
stores run normally. Only HTTP model outputs are fixtures with controlled delay.
There is no supplier key, upstream URL, network-forwarding code or quality claim.
"""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from acceptance.common import read, sha, write


def schema_value(schema, defs=None):
    defs = defs or schema.get("$defs", {})
    if "$ref" in schema:
        return schema_value(defs[schema["$ref"].split("/")[-1]], defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return schema_value(next((v for v in schema["anyOf"] if v.get("type") != "null"), schema["anyOf"][0]), defs)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {k: schema_value(v, defs) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return [schema_value(schema["items"], defs) for _ in range(schema.get("minItems", 0))]
    if kind in {"number", "integer"}:
        return 1
    if kind == "boolean":
        return False
    return "offline fixture" if kind == "string" else None


def content(body, service, image_fixture):
    prompt = json.dumps(body.get("input", []), ensure_ascii=False)
    schema = body.get("text", {}).get("format", {}).get("schema")
    if schema:
        fields = schema.get("properties", {})
        value = schema_value(schema)
        if "document_type" in fields:
            value = {"document_type": "order", "fields": [{"field": key, "value": item["value"], "unit": item.get("unit"), "kind": item.get("kind", "unknown"), "readability": "clear", "location": "authored load fixture"} for key, item in image_fixture.items()]}
        elif "route" in fields:
            current = prompt.split("当前问题：")[-1]
            value.update(route="database_query" if re.search(r"SO-2026-[A-Z]?\d+", current) else "data_qa", confidence=1, reason="Explicit infrastructure fixture")
        elif "intent" in fields:
            values = fields["intent"].get("enum", [])
            current = prompt.split("用户消息:")[-1]
            chosen = "order_status" if re.search(r"SO-2026-[A-Z]?\d+", current) else "query"
            value.update(intent=chosen, confidence=1, reasoning="Explicit infrastructure fixture")
        elif "order" in fields and "sufficient" in fields:
            value.update(order=[0, 1, 2], sufficient=True, missing="")
        return json.dumps(value, ensure_ascii=False), "schema:" + body["text"]["format"].get("name", "unknown")
    if service == "sql":
        if "字段解析助手" in prompt:
            if "电话" in prompt or "手机号" in prompt:
                return "解析结果：订单号，联系电话\n映射术语：orders，order_no，phone", "sql_keywords"
            return "解析结果：订单号，合同金额，订单状态，型号，数量\n映射术语：订单，订单明细，交付日期", "sql_keywords"
        if "语义一致性审阅助手" in prompt:
            return "结论：一致\n理由：基础设施模拟供应商，不作模型语义质量断言。", "sql_self_check"
        if "SQL审阅助手" in prompt:
            return "复述：按指定订单号查询订单主表与明细的状态、金额、型号、数量和日期。", "sql_paraphrase"
        section = prompt.split("# 用户Query")[-1].split("# Schema")[0] if "# 用户Query" in prompt else prompt.split("# 当前步骤CoT")[-1].split("# Schema")[0]
        found = re.findall(r"SO-2026-[A-Z]?\d+", section)
        order = found[0] if found else image_fixture["order_ref"]["value"]
        if "SQL生成助手" in prompt or "SQL修复" in prompt:
            if "phone" in section or "电话" in section or "手机号" in section:
                return f"SELECT order_no,phone FROM orders WHERE order_no='{order}'", "sql_code"
            return f"SELECT o.order_no,o.contract_amount,o.order_status,o.delivery_date,i.model,i.quantity AS ordered_qty FROM orders o JOIN order_item i ON o.order_no=i.order_no WHERE o.order_no='{order}'", "sql_code"
        if "CoT" in prompt or "四元组" in prompt:
            if "手机号" in section or "电话" in section or "phone" in section:
                return f"步骤1：\n(数据库：erp_db，\n处理对象：orders，\n操作指令：按订单号 {order} 查询手机号原值，保留NULL，\n输出目标：orders.order_no，orders.phone，\n预期输出量级：一条)\n", "sql_plan"
            return f"步骤1：\n(数据库：erp_db，\n处理对象：orders，order_item，\n操作指令：按订单号 {order} 联结订单和明细，逐条输出，\n输出目标：orders.order_no，orders.contract_amount，orders.order_status，orders.delivery_date，order_item.model，order_item.quantity，\n预期输出量级：一条)\n", "sql_plan"
        return "基础设施测试已取得业务记录。", "sql_other"
    if "改写" in prompt:
        found = re.findall(r"SO-2026-\d+", prompt)
        return ("查询订单 " + found[-1] + " 的合同金额和订单状态。") if found else "请查询 Cedar-01 cabinets 的配送费用政策。", "helpdesk_rewrite"
    if "重排" in prompt or "相关性" in prompt:
        return "[0, 1, 2]", "helpdesk_rerank"
    return "已完成资料核对，具体业务记录由系统提供。\n[[handoff:no]]", "helpdesk_answer"


def app_for(cfg, delay):
    app = FastAPI()
    corpus = read(cfg["corpus"])
    fixture = next(s["truth"]["image_fields"] for s in corpus["scenarios"] if s["group"] == "images")
    active, maximum, count = 0, 0, 0
    kinds = Counter()
    requests = Path(cfg["root"]) / "simulated-requests"
    @app.get("/health")
    async def health():
        return {"healthy": True, "provider_enabled": False, "provider_mode": "simulation"}
    @app.get("/stats")
    async def stats():
        return {"simulated": True, "supplier_requests": 0, "requests": count, "active": active, "max_active": maximum, "kinds": dict(kinds), "fixed_delay_seconds": delay}
    @app.post("/{service}/v1/responses")
    async def responses(service: str, request: Request):
        nonlocal active, maximum, count
        if request.headers.get("authorization") != "Bearer " + cfg["gateway_token"]:
            raise HTTPException(401)
        body = await request.json()
        active += 1
        maximum = max(maximum, active)
        count += 1
        text, kind = content(body, service, fixture)
        kinds[kind] += 1
        # Keep synthetic request structure for verifying exercised stages. Image
        # base64 is redacted; authored pixels remain in the corpus.
        redacted = re.sub(r"data:image/[^\" ]+", "data:image/REDACTED", json.dumps(body, ensure_ascii=False))
        write(requests / f"{count:06d}.json", {"service": service, "kind": kind, "request_sha256": sha(body), "request": json.loads(redacted)})
        try:
            await asyncio.sleep(delay)
            mid = "msg_" + uuid.uuid4().hex
            output = {"id": mid, "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}
            result = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()), "status": "completed", "model": "explicit-infrastructure-simulation",
                      "output": [output], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
            if body.get("stream"):
                async def stream():
                    events = [{"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": text},
                              {"type": "response.completed", "response": result}]
                    for index, event in enumerate(events):
                        yield "data: " + json.dumps({**event, "sequence_number": index}, ensure_ascii=False) + "\n\n"
                return StreamingResponse(stream(), media_type="text/event-stream")
            return result
        finally:
            active -= 1
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=.8)
    args = parser.parse_args()
    cfg = read(args.config)
    import uvicorn
    uvicorn.run(app_for(cfg, args.delay), host=os.getenv("ACCEPTANCE_BIND_HOST", "127.0.0.1"), port=cfg["gateway_port"], access_log=False, log_level="warning")
