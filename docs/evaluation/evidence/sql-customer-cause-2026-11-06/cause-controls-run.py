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
report = None
try:
    with use_run(RunContext(run_id='customer-real-rerun', sink=sink)), customer_runner() as runner:
        from setup_controls import configure, check
        configure(runner, root, metadata)
        metadata['embedding_status'] = runner.pipeline.schema_retrieval_service.embedding_status
        persist('metadata.json', metadata)
        if metadata['embedding_status'].get('backend') != 'remote' or metadata['embedding_status'].get('fallback'):
            raise RuntimeError('Configured remote embeddings unavailable; no fallback run allowed')
        original_item = runner._run_item
        original_pipeline_run = runner.pipeline.run
        def pipeline_run(*args, **kwargs):
            result = original_pipeline_run(*args, **kwargs)
            step_evidence.append({'id':current, 'steps':[
                {'database':log.database, 'sql':log.final_sql or log.sql, 'execution_result':log.execution_result,
                 'validation':log.validation} for log in result.step_logs],
                'degraded':getattr(result,'degraded',False), 'cot_output':result.cot_output})
            persist('step-evidence.json', step_evidence)
            return result
        runner.pipeline.run = pipeline_run
        def run_item(item):
            global current
            current = item['id']
            before = time.perf_counter()
            request_start = len(requests)
            with use_trace_id():
                result = original_item(item)
                usage = usage_summary()
            data = asdict(result)
            data['cause_coverage'] = check(item, step_evidence[-1])
            data['duration_seconds'] = round(time.perf_counter()-before, 3)
            data['usage'] = usage
            data['gold_rows'] = runner.gold_executor.execute(item['gold_sql'])
            data['alternative_gold_rows'] = [runner.gold_executor.execute(sql) for sql in item.get('gold_sql_alternatives', [])]
            data['requests'] = dict(Counter(r['kind'] for r in requests[request_start:]))
            items.append(data)
            persist('items-progress.json', items)
            print(json.dumps({'id':item['id'], 'correct':result.correct, 'mismatch':result.mismatch,
                              'route':result.route, 'degraded':result.degraded, 'seconds':data['duration_seconds'],
                              'generation_requests':generation_requests, 'requests':data['requests']},ensure_ascii=False),flush=True)
            return result
        runner._run_item = run_item
        report = runner.run(ids=metadata.get('ids'))
        persist('report.json',report.to_dict())
        if report.incomplete:
            metadata['incomplete'] = True
        else:
            for line in customer_summary(report):
                print(line,flush=True)
except BaseException as exc:
    metadata['incomplete'] = True
    metadata['exception_type'] = type(exc).__name__
    print('STOPPED: '+type(exc).__name__,flush=True)
    raise
finally:
    httpx.Client.send = original_send
    metadata['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    metadata['elapsed_seconds'] = round(time.perf_counter()-started,3)
    metadata['generation_requests'] = generation_requests
    metadata['request_counts'] = dict(Counter(r['kind'] for r in requests))
    metadata['items_observed'] = len(items)
    persist('metadata.json',metadata)
    persist('requests.json',requests)
    persist('model-events.json',events)
if report and report.incomplete:
    sys.exit(2)
