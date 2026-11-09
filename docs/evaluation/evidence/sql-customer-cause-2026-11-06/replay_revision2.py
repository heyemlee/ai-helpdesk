import hashlib,json,os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
root=Path(__file__).resolve().parent
os.chdir(root/'final-source/ai-sql-agent')
sys.path[:0]=[str(root/'final-source/ai-sql-agent/src'),str(root/'final-source/ai-sql-agent')]
from benchmark.customer import build_simple_erp,CUSTOMER_CASES
from benchmark.runner import BenchmarkRunner
from ai_sql_agent.semantic import load_semantic_dir
from ai_sql_agent.pipeline.objects import DatabaseConfig,StepExecutionLog
from ai_sql_agent.core.transport import ModelTransport
# No generation, embedding, routing or validation calls during replay.
def forbid(*a,**k):raise AssertionError('Model transport forbidden in captured-output replay')
ModelTransport.post_json=forbid
baseline=Path('/Users/l/Desktop/Projects/ai-helpdesk/docs/evaluation/evidence/sql-customer-2026-11-06')
e={'step_results':json.loads((root/'full/step-evidence.json').read_text())};old=json.loads((root/'full/report.json').read_text())
observations={x['id']:x for x in e['step_results']}
by_query={x['query']:x for x in old['items']}
with tempfile.TemporaryDirectory() as tmp:
 layer=load_semantic_dir(build_simple_erp(Path(tmp)))
 class RecordedPipeline:
  databases=[DatabaseConfig(name=d.name,db_path=d.db_path) for d in layer.databases.values()]
  business_metas={}
  def run(self,query):
   record=observations[by_query[query]['id']]
   logs=[StepExecutionLog(database=s['database'],cot_step=None,local_schema='',sql=s['sql'],execution_request={},execution_result=s['execution_result'],validation=s['validation']) for s in record['steps']]
   return SimpleNamespace(step_logs=logs,degraded=record['degraded'],retrieved_columns=[],prompt_columns=[])
 runner=BenchmarkRunner(pipeline=RecordedPipeline(),dataset_path=root/'revision2-cases.json',score_all_steps=True,access_check=lambda:[])
 runner._route=lambda query:(by_query[query]['route'],by_query[query]['route_signals'])
 try:report=runner.run().to_dict()
 finally:runner.gold_executor.close()
(root/'full-revision2-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
changed=[{'id':a['id'],'before':a['correct'],'after':b['correct']} for a,b in zip(old['items'],report['items']) if a['correct']!=b['correct']]
summary={'full_revision3_score':old['correct'],'full_revision2_score':report['correct'],'total':report['total'],'changed':changed,'real_provider_calls':0,'full_evidence_sha256':hashlib.sha256((root/'full/step-evidence.json').read_bytes()).hexdigest(),'revision2_dataset_sha256':hashlib.sha256((root/'revision2-cases.json').read_bytes()).hexdigest(),'revision2_rubric_sha256':hashlib.sha256((root/'revision2-rubric.md').read_bytes()).hexdigest()}
assert changed==[]
(root/'full-revision2-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
