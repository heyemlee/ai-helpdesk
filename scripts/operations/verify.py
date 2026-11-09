#!/usr/bin/env python3
"""Scheduled restore verification. Never starts restored applications or webhooks.

Decrypts and restores into a new disposable PostgreSQL project, compares every
business table digest and private-object hash, checks SQL SQLite integrity, and
asserts restored accounts/sessions stay closed. Release HTTP smoke is separate.
"""
import argparse
import fcntl
import json
from pathlib import Path
import secrets
import shutil
import tempfile
import time

from recovery import Operations, atomic_json, digest, publish_metric_status


def verify(config, *, source_lock_held=False):
    source = json.loads(Path(config).read_text())
    if source_lock_held:
        return _verify(source)
    # Serialize with backup/retention while the selected artifact is in use.
    with (Path(source['backup_dir'])/'operations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _verify(source)


def _verify(source):
    backup_dir = Path(source['backup_dir'])
    latest = json.loads((backup_dir/'last-backup.json').read_text())
    artifact = latest['artifact']
    started = time.time()
    with tempfile.TemporaryDirectory(prefix='aihd-stage5-verify-') as folder:
        root = Path(folder)
        selected=root/'selected.aihdbak'
        shutil.copyfile(artifact,selected)
        actual_sha256=digest(selected)
        if actual_sha256 != latest['sha256']:
            raise ValueError('Selected backup differs from its recorded ciphertext digest')
        for name in ['data/objects','data/sql','data/config','backups','reports','gate']:
            (root/name).mkdir(parents=True)
        (root/'app.env').write_text('APP_ENV=development\n')
        (root/'sql.env').write_text('AI_SQL_APP_ENV=development\n')
        env = {'BACKEND_IMAGE':'verification-never-started','SQL_IMAGE':'verification-never-started','FRONTEND_IMAGE':'verification-never-started',
               'APP_ENV_FILE':str(root/'app.env'),'SQL_ENV_FILE':str(root/'sql.env'),'STATE_ROOT':str(root/'data'),
               'REPORT_ROOT':str(root/'reports'),'GATE_ROOT':str(root/'gate'),'POSTGRES_PASSWORD':secrets.token_urlsafe(24),
               'PUBLIC_ORIGIN':'http://127.0.0.1:1','PUBLIC_HOST':'employees.invalid','CONSOLE_PORT':'1'}
        (root/'release.env').write_text('\n'.join(k+'='+v for k,v in env.items())+'\n')
        cfg = {'environment':'drill','isolation_root':str(root),'project':'aihd-stage5-verify-'+secrets.token_hex(5),
               'compose_file':str(Path(__file__).resolve().parents[2]/'deploy/release/compose.yml'),
               'env_file':str(root/'release.env'),'data_root':str(root/'data'),'gate_root':str(root/'gate'),
               'backup_dir':str(root/'backups'),'key_file':source['key_file'],'storage_services':['postgres','redis']}
        path=root/'operations.json';atomic_json(path,cfg)
        ops=Operations(path)
        try:
            ops.run('up','-d','--wait','postgres')
            restored=ops.restore(selected)
            if restored['snapshot_at'] != latest['snapshot_at']:
                raise ValueError('Selected backup snapshot time differs from its catalog')
            assert ops.sql('SELECT count(*) FROM staff_sessions') == '0'
            assert ops.sql('SELECT count(*) FROM staff_accounts WHERE active') == '0'
            assert not set(ops.running()) & set(ops.writers)
            result={'verified_at':time.time(),'elapsed_seconds':time.time()-started,'snapshot_at':restored['snapshot_at'],
                    'artifact_sha256':actual_sha256,'business':restored['business'],'closed_accounts':True,'applications_started':False}
            atomic_json(backup_dir/'last-verification.json',result)
            publish_metric_status(source,'last-verification.json','verified_at',result['verified_at'])
            (backup_dir/'restore_verification.prom').write_text(f'aihd_restore_verification_last_success_timestamp_seconds {time.time()}\n')
            return result
        finally:
            ops.run('down','-v','--remove-orphans',check=False)
            ops.lock.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    print(json.dumps(verify(parser.parse_args().config)))


if __name__=='__main__':main()
