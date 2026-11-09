import os
import sys
import json
import time
import hashlib
from pathlib import Path
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone

root = Path(__file__).resolve().parent
os.chdir(root / 'ai-sql-agent')
for key in list(os.environ):
    if key.startswith(('AI_SQL_', 'OPENAI_', 'ANTHROPIC_', 'DASHSCOPE_', 'OTEL_', 'MODEL_')):
        del os.environ[key]
config = json.loads((root / 'model-config.json').read_text())
for key, value in config.items():
    os.environ['AI_SQL_' + key.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
os.environ['AI_SQL_MODEL_SETTINGS_ENABLED'] = 'false'
os.environ['AI_SQL_PGVECTOR_DSN'] = ''
os.environ['AI_SQL_MODEL_SETTINGS_PATH'] = str(root / 'unused-model-settings.json')
sys.path[:0] = [str(root / 'ai-sql-agent/src'), str(root / 'ai-sql-agent')]

import httpx
from ai_sql_agent.core import get_settings, usage_summary
from ai_sql_agent.core.trace import use_trace_id
from ai_sql_agent.core.run_context import RunContext, use_run
from benchmark.customer import customer_runner, customer_summary

class RequestBudgetExceeded(BaseException):
    pass

started = time.perf_counter()
requests = []
items = []
events = []
step_evidence = []
current = 'initialization'
original_send = httpx.Client.send
generation_requests = 0

def persist(name, value):
    target = root / name
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temporary.replace(target)

def send(self, request, *args, **kwargs):
    global generation_requests
    path = request.url.path
    generation = path.endswith(('/chat/completions', '/responses', '/messages'))
    if generation:
        if generation_requests >= metadata['generation_request_safety_cap']:
            raise RequestBudgetExceeded('Generation request safety cap reached')
        generation_requests += 1
    record = {'item_id': current, 'kind': 'generation' if generation else 'embedding' if path.endswith('/embeddings') else 'other',
              'path': path, 'host': request.url.host, 'elapsed_start_seconds': round(time.perf_counter()-started, 3)}
    try:
        payload = json.loads(request.content)
        record['requested_model'] = payload.get('model')
    except Exception:
        pass
    before = time.perf_counter()
    try:
        response = original_send(self, request, *args, **kwargs)
        record['status_code'] = response.status_code
        if not response.is_stream_consumed:
            response.read()
        try:
            body = response.json()
            record['returned_model'] = body.get('model')
            record['usage'] = body.get('usage')
            choices = body.get('choices') or []
            if choices:
                record['finish_reason'] = choices[0].get('finish_reason')
        except Exception:
            pass
        return response
    except Exception as exc:
        record['error_type'] = type(exc).__name__
        raise
    finally:
        record['duration_ms'] = round((time.perf_counter()-before)*1000, 2)
        requests.append(record)
        with (root/'requests.jsonl').open('a') as f:
            f.write(json.dumps(record, ensure_ascii=False)+'\n')

httpx.Client.send = send

def sink(event_type, stage, step, attrs):
    if event_type.startswith('model.'):
        event = {'item_id':current, 'type':event_type, 'stage':stage, 'step':step, **attrs}
        events.append(event)
        with (root/'model-events.jsonl').open('a') as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str)+'\n')

metadata = json.loads((root/'metadata.json').read_text())
metadata['started_at_utc'] = datetime.now(timezone.utc).isoformat()
metadata['dataset_sha256'] = hashlib.sha256(Path('benchmark/customer_cases.json').read_bytes()).hexdigest()
metadata['import_path'] = __import__('ai_sql_agent').__file__
persist('metadata.json', metadata)
from ai_sql_agent.planning import CotPlanner, few_shots_for
schema="""数据库 orders_db: orders(order_no TEXT 主键，order_status TEXT 当前整体状态，planned_date TEXT 计划交付日期)
数据库 production_db: jobs(order_no TEXT 主键，production_notes TEXT 生产问题、暂停原因)
数据库 shipping_db: deliveries(order_no TEXT，delivery_notes TEXT 配送受阻原因)
跨库 order_no 表示同一订单，禁止跨库 JOIN。"""
controls=[
 ('overall_only','订单 X-710 的当前状态是什么？',['orders_db']),
 ('production_only','订单 X-710 的生产暂停原因是什么？只看生产记录。',['production_db']),
 ('delivery_only','What delivery problem is recorded for X-710?',['shipping_db']),
 ('explicit_duration','订单 X-710 的计划交付日距离 2026-11-06 相差多少天？',['orders_db'])]
results=[]
with use_run(RunContext(run_id='cause-planning-controls',sink=sink)):
 for current,query,expected in controls:
  with use_trace_id():
   result=CotPlanner().plan(query,schema,few_shots=few_shots_for(3))
   usage=usage_summary()
  databases=[s.database for s in result.steps]
  passed=databases==expected
  if current=='explicit_duration':
   passed=passed and '2026-11-06' in result.raw_output and any(t in result.raw_output for t in ['相差','差值','天数','日期差'])
  results.append({'id':current,'query':query,'expected_databases':expected,'databases':databases,'passed':passed,'raw_output':result.raw_output,'usage':usage})
  persist('controls.json',results)
  print(current,passed,databases,flush=True)
metadata['finished_at_utc']=datetime.now(timezone.utc).isoformat()
metadata['elapsed_seconds']=round(time.perf_counter()-started,3)
metadata['generation_requests']=generation_requests
metadata['request_counts']=dict(Counter(x['kind'] for x in requests))
metadata['passed']=sum(x['passed'] for x in results)
persist('metadata.json',metadata);persist('requests.json',requests);persist('model-events.json',events)
assert all(x['passed'] for x in results)
