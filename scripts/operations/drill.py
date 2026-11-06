#!/usr/bin/env python3
"""Real disposable Compose recovery/release drill. Never uses production env or data."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request

from recovery import Operations, atomic_json, digest
from release import deploy
from verify import verify

ROOT = Path(__file__).resolve().parents[2]


def sandbox(manifest, folder=None):
    root = Path(folder or tempfile.mkdtemp(prefix='aihd-stage5-drill-'))
    for name in ['data/objects','data/config','data/sql','backups','reports','gate']:
        (root/name).mkdir(parents=True, exist_ok=True)
        (root/name).chmod(0o777 if name.startswith('data/') else 0o700)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    password=secrets.token_urlsafe(24)
    (root/'backup-key').write_text(base64.b64encode(os.urandom(32)).decode());(root/'backup-key').chmod(0o600)
    (root/'app.env').write_text('APP_ENV=development\nLLM_PROVIDER=openai\nOPENAI_API_KEY=fixture-key\nOPENAI_BASE_URL=http://127.0.0.1:9/v1\nOPENAI_MODEL=fixture\nAI_HELPDESK_API_KEY=fixture-service\nAI_HELPDESK_AUTH_SECRET=fixture-session-signing-secret-only\nAI_HELPDESK_PROTECT_CHAT=true\nAI_HELPDESK_DEPLOYMENT_SCOPE=internal_single_tenant\nSQL_AGENT_API_KEY=fixture-sql-read\nRECOVERY_DRILL=1\nAI_HELPDESK_RUN_DRAIN_S=2\nTICKET_WEBHOOK_URL=\nALERT_WEBHOOK_URL=\n')
    (root/'sql.env').write_text('AI_SQL_APP_ENV=development\nOPENAI_API_KEY=fixture-key\nOPENAI_BASE_URL=http://127.0.0.1:9/v1\nAI_SQL_DEPLOYMENT_SCOPE=internal_single_tenant\n')
    (root/'data/config/sql_keys.json').write_text(json.dumps({'keys':[{'key':'fixture-sql-read','caller':'ai-helpdesk','user_id':'ai-helpdesk'},{'key':'fixture-sql-admin','caller':'console','user_id':'console','is_admin':True}]}))
    (root/'data/config/sql_admin_key').write_text('fixture-sql-admin')
    (root/'data/config/metrics_token').write_text('fixture-metrics-token')
    release=json.loads(Path(manifest).read_text())
    env={'BACKEND_IMAGE':release['images']['backend'],'SQL_IMAGE':release['images']['sql'],'FRONTEND_IMAGE':release['images']['frontend'],
         'APP_ENV_FILE':str(root/'app.env'),'SQL_ENV_FILE':str(root/'sql.env'),'STATE_ROOT':str(root/'data'),'REPORT_ROOT':str(root/'reports'),
         'GATE_ROOT':str(root/'gate'),'POSTGRES_PASSWORD':password,'PUBLIC_ORIGIN':f'http://127.0.0.1:{port}',
         'PUBLIC_HOST':'employees.invalid','CONSOLE_PORT':str(port)}
    (root/'release.env').write_text('\n'.join(k+'='+v for k,v in env.items())+'\n');(root/'release.env').chmod(0o600)
    cfg={'environment':'drill','isolation_root':str(root),'project':'aihd-stage5-'+secrets.token_hex(5),'compose_file':str(ROOT/'deploy/release/compose.yml'),
         'env_file':str(root/'release.env'),'data_root':str(root/'data'),'gate_root':str(root/'gate'),'backup_dir':str(root/'backups'),
         'report_root':str(root/'reports'),
         'key_file':str(root/'backup-key'),'storage_services':['postgres','redis'],'release':release,
         'config_files':[str(root/'app.env'),str(root/'sql.env'),str(root/'release.env')]}
    atomic_json(root/'operations.json',cfg)
    return root, cfg, password, port


def assert_isolated(ops, root):
    spec=json.loads(ops.run('config','--format','json').stdout)
    for name, service in spec['services'].items():
        for volume in service.get('volumes',[]):
            if volume['type']=='bind':
                path=Path(volume['source']).resolve()
                if not path.is_relative_to(root.resolve()) and path != ROOT/'deploy/release/ingress.conf':
                    raise AssertionError('Non-isolated bind: '+str(path))
        for port in service.get('ports',[]):
            assert port['host_ip']=='127.0.0.1' and str(port['published']) not in {'5433','6379','8080','8081','5174'}
    for volume in spec.get('volumes',{}).values():
        assert volume['name'].startswith(ops.project+'_') and not volume.get('external')


def enable_s3(root, cfg):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    (root/'s3').mkdir();(root/'s3').chmod(0o777)
    credentials=root/'data/config/object_credentials.json'
    atomic_json(credentials,{'aws_access_key_id':'stage5-fixture','aws_secret_access_key':'stage5-fixture-secret','region_name':'us-east-1'})
    override=root/'s3-compose.json'
    atomic_json(override,{'services':{'objects':{'image':'chrislusf/seaweedfs:4.52','command':['mini','-dir=/data'],
        'environment':{'AWS_ACCESS_KEY_ID':'stage5-fixture','AWS_SECRET_ACCESS_KEY':'stage5-fixture-secret'},
        'ports':[f'127.0.0.1:{port}:8333'],'volumes':[str(root/'s3')+':/data'],'networks':['private','outbound']}}})
    cfg['compose_overrides']=[str(override)]
    cfg['storage_services'].append('objects')
    spec={'endpoint':f'http://127.0.0.1:{port}','credentials_file':str(credentials)}
    cfg['objects_s3']={**spec,'bucket':'stage5-private-attachments'}
    cfg['offsite']={**spec,'bucket':'stage5-private-backups','prefix':'snapshots/'}
    cfg['require_offsite']=True
    with (root/'release.env').open('a') as file:
        file.write('OBJECT_STORAGE=s3\n')
    with (root/'app.env').open('a') as file:
        file.write('AI_HELPDESK_ATTACHMENT_BUCKET=stage5-private-attachments\nAI_HELPDESK_ATTACHMENT_ENDPOINT=http://objects:8333\nAWS_ACCESS_KEY_ID=stage5-fixture\nAWS_SECRET_ACCESS_KEY=stage5-fixture-secret\nAWS_DEFAULT_REGION=us-east-1\n')
    atomic_json(root/'operations.json',cfg)


def public_boundary(port):
    # Exercise the actual published loopback port, not only container networking.
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/api/auth/session',timeout=5)
    except urllib.error.HTTPError as error:
        if error.code == 401:return True
        raise
    raise AssertionError('Public ingress accepted an anonymous employee session')


def closed_boundary(port):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/api/auth/session',timeout=5)
    except urllib.error.HTTPError as error:
        if error.code == 503:return True
        raise
    except urllib.error.URLError as error:
        if isinstance(error.reason,(ConnectionRefusedError,ConnectionResetError)):return True
        raise
    raise AssertionError('Maintenance ingress remained open')


SQL_COUNTS = "import sqlite3,json; result={};\nfor name in ['audit','runs']:\n c=sqlite3.connect('file:/state/sql/'+name+'.db?mode=ro',uri=True);tables=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'\")];result[name]={t:c.execute('SELECT count(*) FROM \"'+t+'\"').fetchone()[0] for t in tables}\nprint(json.dumps(result))"


def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--report',required=True);p.add_argument('--legacy-image');p.add_argument('--s3',action='store_true')
    p.add_argument('--case-count',type=int,default=12)
    args=p.parse_args();root,cfg,password,port=sandbox(args.manifest)
    if args.s3:enable_s3(root,cfg)
    ops=Operations(root/'operations.json');report={'started_at':time.time(),'project':ops.project,'root':str(root),'object_backend':'s3-compatible-local-fixture' if args.s3 else 'local-private-files',
        'release_manifest_sha256':digest(args.manifest),'tested_image_ids':json.loads(Path(args.manifest).read_text())['images'],
        'operations_source_sha256':{str(file.relative_to(ROOT)):digest(file) for file in sorted([*Path(__file__).parent.glob('*.py'),* (ROOT/'deploy/release').glob('*')]) if file.is_file()}}
    try:
        assert_isolated(ops,root);report['resource_isolation_verified']=True
        ops.run('up','-d','--wait',*cfg['storage_services'])
        if args.s3:
            client=ops.s3(cfg['objects_s3'])
            for attempt in range(30):
                try:client.list_buckets();break
                except Exception:
                    if attempt==29:raise
                    time.sleep(1)
            for spec in [cfg['objects_s3'],cfg['offsite']]:client.create_bucket(Bucket=spec['bucket'])
        seeded=ops.run('run','--rm','--no-deps','-T','api','python','scripts/recovery_fixture.py', input=json.dumps({'password':password,'case_count':args.case_count}).encode())
        fixture=json.loads(seeded.stdout.decode().splitlines()[-1])
        if args.s3:
            for file in (root/'data/objects').rglob('*'):
                if file.is_file():client.upload_file(str(file),cfg['objects_s3']['bucket'],str(file.relative_to(root/'data/objects')))
        probe={'origin':f'http://127.0.0.1:{port}','login':'recovery-employee','password':password,'case_id':fixture['case_id'],'facts':{'order_ref':'RECOVERY-001'},'attachment_previews':fixture['attachment_previews']}
        atomic_json(root/'probe.json',probe);atomic_json(root/'access-review.json',fixture['accounts'])
        report['initial_release']=deploy(ops,args.manifest,root/'probe.json')
        report['public_ingress_auth_verified']=public_boundary(port)
        report['fixture_scale']={'case_count':fixture['case_count'],'attachment_count':len(fixture['attachment_previews']),
                                 'extraction_count':fixture['extraction_count'],'quality_claim':False}
        if args.case_count >= 200:
            # A real SQL Agent HTTP request with the real run/audit stores. Its
            # supplier is explicitly closed on localhost:9; a recorded failure
            # is enough to test persistence and is never a quality pass.
            ops.run('exec','-T','api','python','-c',
                "import os,httpx; r=httpx.post('http://sql-agent:8000/query',headers={'X-API-Key':os.environ['SQL_AGENT_API_KEY']},json={'query':'查询订单 RECOVERY-001 的订单状态','user_id':'ai-helpdesk','session_id':'recovery-scale-audit'},timeout=90);assert r.status_code not in {401,403}")
            rows=ops.run('exec','-T','sql-agent','python','-c',SQL_COUNTS)
            report['sql_state_before_backup']=json.loads(rows.stdout)
            assert any(report['sql_state_before_backup']['audit'].values()) and any(report['sql_state_before_backup']['runs'].values()), 'SQL audit/run workload was empty'
        snapshot=ops.backup();report['backup']=snapshot
        # A fully independent PG restore, including authentication invalidation.
        report['scheduled_verification']=verify(root/'operations.json',source_lock_held=True)
        # Verify real metrics from the non-root API, with timestamp-only reports.
        ops.run('exec','-T','api','python','-c',
            "import os,pathlib,httpx; token=pathlib.Path(os.environ['AI_HELPDESK_METRICS_TOKEN_FILE']).read_text();r=httpx.get('http://localhost:8000/metrics',headers={'Authorization':'Bearer '+token});r.raise_for_status();values={line.split()[0]:float(line.split()[1]) for line in r.text.splitlines() if line.startswith(('aihd_backup_last_success_timestamp_seconds ','aihd_restore_verification_last_success_timestamp_seconds '))};assert values['aihd_backup_last_success_timestamp_seconds']>0 and values['aihd_restore_verification_last_success_timestamp_seconds']>0")
        report['nonroot_api_reads_backup_verification_metrics']=True
        ops.close_writers('drill_fault_preparation')
        report['maintenance_public_port_closed_verified']=closed_boundary(port)
        # Corrupted ciphertext must never start writers or mutate the database.
        bad=root/'corrupt.aihdbak';data=bytearray(Path(snapshot['artifact']).read_bytes());data[64]^=1;bad.write_bytes(data)
        before=ops.business()
        try: ops.restore(bad)
        except Exception: pass
        else: raise AssertionError('Corrupt backup accepted')
        assert ops.business()==before and not set(ops.running())&set(ops.writers) and ops.gate.exists()
        report['corrupt_snapshot_fail_closed']=True
        # Real failure AFTER PostgreSQL restore: a file blocks the config directory.
        ops.sql("UPDATE policy_documents SET title='changed after snapshot'")
        saved=root/'config-before-fault';(root/'data/config').rename(saved)
        (root/'data/config').write_text('injected storage fault')
        try: ops.restore(snapshot['artifact'])
        except Exception: pass
        else: raise AssertionError('Injected partial restore unexpectedly succeeded')
        assert ops.business()==snapshot['business']  # PostgreSQL really restored before the failure.
        assert not set(ops.running())&set(ops.writers) and ops.gate.exists()
        report['partial_restore_fail_closed_after_pg_commit']=True
        assert closed_boundary(port)
        (root/'data/config').unlink();saved.rename(root/'data/config')
        if args.s3:
            key=ops.sql("SELECT object_key FROM attachments ORDER BY id LIMIT 1")
            client.put_object(Bucket=cfg['objects_s3']['bucket'],Key=key,Body=b'corrupted-target-object')
            original=ops.s3
            class DroppedUpload:
                def __init__(self,client):self.client=client
                def upload_file(self,*args,**kwargs):pass
                def __getattr__(self,name):return getattr(self.client,name)
            ops.s3=lambda spec:DroppedUpload(original(spec))
            try:
                try:ops.restore(snapshot['artifact'])
                except ValueError as error:
                    assert str(error)=='Restored target object hash mismatch'
                else:raise AssertionError('Silently dropped object restore accepted')
            finally:ops.s3=original
            assert not set(ops.running())&set(ops.writers) and ops.gate.exists()
            report['actual_s3_target_corruption_fail_closed']=True
        # One durable post-snapshot business write is deliberately lost on restore.
        mutation_time=time.time()
        ops.sql("INSERT INTO case_actions(action_key,case_id,kind,data,created_at) SELECT 'post-snapshot-write',id,'drill','{}',extract(epoch FROM now()) FROM tickets LIMIT 1")
        failure_time=time.time()
        report['rpo_observation']={'snapshot_to_simulated_loss_seconds':failure_time-snapshot['snapshot_at'],
            'post_snapshot_write_at':mutation_time,'post_snapshot_writes_expected_lost':1}
        # Restore an actual database+SQL-state+private-object snapshot.
        started=time.time();report['recovery']=ops.restore(snapshot['artifact'])
        if args.case_count >= 200:
            rows=ops.run('run','--rm','--no-deps','-T','--entrypoint','python','sql-agent','-c',SQL_COUNTS)
            report['sql_state_after_restore']=json.loads(rows.stdout)
            assert report['sql_state_after_restore']==report['sql_state_before_backup'], 'SQL audit/run records changed in restore'
            report['restored_extraction_count']=int(ops.sql('SELECT count(*) FROM attachment_extractions'))
            assert report['restored_extraction_count']==fixture['extraction_count']
        assert ops.sql('SELECT count(*) FROM staff_sessions')=='0'
        assert ops.sql('SELECT count(*) FROM staff_accounts WHERE active')=='0'
        assert ops.sql("SELECT count(*) FROM case_actions WHERE action_key='post-snapshot-write'")=='0'
        report['rpo_observation']['post_snapshot_writes_observed_lost']=1
        report['sessions_revoked_and_accounts_closed']=True
        ops.review_access(root/'access-review.json')
        report['reopened_release']=deploy(ops,args.manifest,root/'probe.json')
        assert public_boundary(port)
        report['rto_seconds_including_review_and_http_smoke']=time.time()-started
        report['observed_snapshot_age_seconds']=time.time()-snapshot['snapshot_at']
        # A post-snapshot key rotation must not reopen a stack whose restored SQL
        # key inventory no longer matches the still-current external app.env.
        active_env=(root/'app.env').read_text()
        (root/'app.env').write_text(active_env+'SQL_AGENT_API_KEY=rotated-after-snapshot\n')
        try:
            try:deploy(ops,args.manifest,root/'probe.json')
            except ValueError as error:
                assert str(error)=='Employee SQL gateway smoke failed'
            else:raise AssertionError('Mismatched restored SQL credentials were accepted')
            check=json.loads((ops.output/'last-sql-gateway-smoke.json').read_text())
            assert check['status_code']==502 and not check['passed']
            assert not set(ops.running())&set(ops.writers) and ops.gate.exists() and closed_boundary(port)
            report['rotated_sql_key_fail_closed_through_employee_gateway']=True
        finally:(root/'app.env').write_text(active_env)
        ops.restore(snapshot['artifact']);ops.review_access(root/'access-review.json')
        deploy(ops,args.manifest,root/'probe.json');assert public_boundary(port)
        # Previously published, pre-authentication binary: diagnostic isolation only.
        if args.legacy_image:
            ops.close_writers('legacy_diagnostic')
            # Create a one-off override for the *actual* old image, no project mutation.
            override=root/'legacy.json';atomic_json(override,{'services':{'api':{'image':args.legacy_image}}})
            legacy_before=ops.business()
            original=list(ops.compose);ops.compose += ['-f',str(override)]
            try:
                # Bounded real startup against the restored upgraded PG schema.
                result=ops.run('run','--rm','--no-deps','-T','api','python','-c',
                    "import asyncio;from app.api.main import app;\nasync def probe():\n async with app.router.lifespan_context(app): print('LEGACY_STARTUP_OK')\nasyncio.run(asyncio.wait_for(probe(),30))",check=False)
                report['legacy_binary']={'image':args.legacy_image,'exit_code':result.returncode,'startup_ok':'LEGACY_STARTUP_OK' in result.stdout.decode(),'reopen_allowed':False}
                (root/'legacy-startup.log').write_bytes(result.stdout+result.stderr)
            finally:ops.compose=original
            assert ops.business()==legacy_before
            # Do not bless or reopen the legacy artifact, even if health succeeds.
            atomic_json(ops.marker,{'phase':'awaiting_business_smoke'})
            deploy(ops,args.manifest,root/'probe.json')
        # An immutable deliberately broken candidate must roll back to the tested minimum safe release.
        safe=json.loads(Path(args.manifest).read_text())
        tag='aihd-stage5-fault:'+secrets.token_hex(5)
        # A local image ID is not valid Dockerfile FROM syntax. Create an inert
        # temporary container from the exact tested image and commit its bad CMD.
        container=ops.project+'-fault-image'
        subprocess.run(['docker','create','--name',container,safe['images']['backend']],check=True,stdout=subprocess.DEVNULL)
        try:
            subprocess.run(['docker','commit','--change','CMD ["python", "-c", "raise SystemExit(42)"]',container,tag],check=True,stdout=subprocess.DEVNULL)
        finally:
            subprocess.run(['docker','rm',container],check=True,stdout=subprocess.DEVNULL)
        bad_image=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',tag]).decode().strip()
        broken={**safe,'images':{**safe['images'],'backend':bad_image}};atomic_json(root/'broken-release.json',broken)
        rollback_started=time.time()
        try: deploy(ops,root/'broken-release.json',root/'probe.json')
        except Exception: pass
        else:raise AssertionError('Broken candidate accepted')
        assert json.loads((ops.output/'last-release.json').read_text())['outcome']=='rolled_back'
        assert not ops.gate.exists() and not ops.marker.exists()
        assert public_boundary(port)
        persisted=Path(cfg['env_file']).read_text()
        assert 'BACKEND_IMAGE='+safe['images']['backend'] in persisted
        report['failed_release_rolled_back_to_safe_images']=True
        report['failed_candidate_image']=bad_image
        report['rollback_seconds_including_candidate_failure']=time.time()-rollback_started
        report['persisted_release_matches_running']=True
        report['completed_at']=time.time();atomic_json(args.report,report)
        print(json.dumps(report))
    except BaseException as error:
        report['failed_with']=type(error).__name__;atomic_json(args.report,report)
        (root/'failure-services.log').write_bytes(ops.run('logs','--no-color','--tail','100',check=False).stdout)
        raise
    finally:
        ops.run('down','-v','--remove-orphans',check=False);ops.lock.close()
        # Retain only isolated artifacts for review; never delete source or production resources.
        print('Isolated drill artifacts: '+str(root))


if __name__=='__main__':main()
