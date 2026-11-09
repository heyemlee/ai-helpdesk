#!/usr/bin/env python3
"""Closed-writer encrypted backup and fail-closed recovery for an explicit Compose project.

Requires an operator-owned JSON configuration; never discovers production .env.
Restoration never restarts writers. Access review and a separate release smoke
are mandatory before reopening. The database and object snapshot are inseparable.
"""
from __future__ import annotations
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BUSINESS_TABLES = ['tickets','ticket_turns','ticket_notifications','ticket_case_state','case_messages','case_materials','case_drafts','case_actions','case_run_receipts','case_events','ticket_notification_claims','attachments','attachment_extractions','case_image_reviews','policy_documents','policy_chunks','agent_runs','conversation_log','conversation_log_turns','staff_conversations','staff_sql_runs']
MAGIC = b'AIHDBAK1'
CHUNK = 1024 * 1024


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for part in iter(lambda: source.read(CHUNK), b''):
            h.update(part)
    return h.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2))
    temp.chmod(0o600)
    os.replace(temp, path)


def publish_metric_status(config, name, field, timestamp):
    # Only timestamps are readable by the non-root API. The encrypted archives,
    # environment copies and full operator reports remain in the private folder.
    if not config.get('report_root'):
        return
    directory=Path(config['report_root']).resolve()
    if any(directory.is_relative_to(Path(config[key]).resolve()) for key in ('backup_dir','data_root') if config.get(key)):
        raise ValueError('Readable reports cannot expose private directories')
    directory.mkdir(parents=True,exist_ok=True,mode=0o755)
    directory.chmod(0o755)
    path=directory/name
    atomic_json(path,{field:timestamp})
    path.chmod(0o644)


def crypt(source, destination, key_file, *, decrypt=False):
    key = base64.b64decode(Path(key_file).read_text().strip(), validate=True)
    if len(key) != 32 or Path(key_file).stat().st_mode & 0o077:
        raise ValueError('Backup key must contain 32 random bytes in base64 and have mode 0600')
    with Path(source).open('rb') as inp, Path(destination).open('wb') as out:
        os.chmod(destination, 0o600)
        if decrypt:
            if inp.read(8) != MAGIC:
                raise ValueError('Invalid encrypted snapshot')
            nonce = inp.read(12)
            size = Path(source).stat().st_size - 36
            if size < 0:
                raise ValueError('Truncated snapshot')
            inp.seek(-16, 2); tag = inp.read(16); inp.seek(20)
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        else:
            nonce = os.urandom(12)
            out.write(MAGIC + nonce)
            size = Path(source).stat().st_size
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        cipher.authenticate_additional_data(MAGIC)
        while size:
            chunk = inp.read(min(CHUNK, size))
            if not chunk:
                raise ValueError('Truncated snapshot')
            out.write(cipher.update(chunk)); size -= len(chunk)
        out.write(cipher.finalize())
        if not decrypt:
            out.write(cipher.tag)
        out.flush(); os.fsync(out.fileno())


class Operations:
    def __init__(self, config, allow_production=False):
        self.c = json.loads(Path(config).read_text())
        self.project = self.c['project']
        if not re.fullmatch(r'[a-z][a-z0-9-]+', self.project):
            raise ValueError('Explicit Compose project required')
        if self.c.get('environment') != 'drill' and not allow_production:
            raise ValueError('Production operations require an explicitly authorized invocation')
        if self.c.get('environment') == 'drill' and not self.project.startswith('aihd-stage5-'):
            raise ValueError('Disposable drill project prefix required')
        self.root = Path(self.c['data_root']).resolve()
        self.output = Path(self.c['backup_dir']).resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.output.is_relative_to(self.root):
            raise ValueError('Backups must be outside the data directory')
        key_file=Path(self.c['key_file']).resolve()
        if key_file.is_relative_to(self.root) or key_file.is_relative_to(self.output):
            raise ValueError('Encryption key must be outside data and backup directories')
        if self.c.get('report_root'):
            reports=Path(self.c['report_root']).resolve()
            if reports.is_relative_to(self.root) or reports.is_relative_to(self.output):
                raise ValueError('Readable metric reports must be separate from private data/backups')
        self.lock = (self.output/'operations.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.compose = ['docker', 'compose', '--project-name', self.project, '--env-file', self.c['env_file'], '-f', self.c['compose_file']]
        for override in self.c.get('compose_overrides', []):
            self.compose.extend(['-f', override])
        self.pg = self.c.get('postgres_service', 'postgres')
        self.user = self.c.get('postgres_user', 'helpdesk')
        self.database = self.c.get('postgres_database', 'helpdesk')
        self.marker = self.output/'maintenance.json'
        self.gate = Path(self.c['gate_root'])/'maintenance'
        self.gate.parent.mkdir(parents=True, exist_ok=True)
        spec = json.loads(self.run('config', '--format', 'json').stdout)
        if spec.get('name') != self.project or any(s.get('container_name') for s in spec['services'].values()):
            raise ValueError('Only explicitly scoped Compose services without fixed container names are supported')
        if self.c.get('environment') == 'drill':
            isolation = Path(self.c['isolation_root']).resolve()
            allowed_temp = [Path('/tmp').resolve(), Path(tempfile.gettempdir()).resolve()]
            if not any(isolation.is_relative_to(p) and isolation != p for p in allowed_temp):
                raise ValueError('Drills must live in a dedicated temporary directory')
            for service in spec['services'].values():
                for mount in service.get('volumes', []):
                    if mount['type'] == 'bind':
                        src = Path(mount['source']).resolve()
                        ingress = Path(__file__).resolve().parents[2]/'deploy/release/ingress.conf'
                        if not src.is_relative_to(isolation) and not (src == ingress and mount.get('read_only')):
                            raise ValueError('Drill bind escapes its temporary directory')
                for port in service.get('ports', []):
                    if port.get('host_ip') != '127.0.0.1' or str(port.get('published')) in {'5433','6379','8080','8081','5174'}:
                        raise ValueError('Drill port is not isolated')
            if any(v.get('external') or not v.get('name','').startswith(self.project+'_') for v in spec.get('volumes',{}).values()):
                raise ValueError('Drill volumes are not isolated')
        # Stop every application/consumer before snapshot, not just the HTTP API.
        self.writers = sorted(set(spec['services']) - set(self.c['storage_services']))
        if not self.writers or self.pg not in self.c['storage_services']:
            raise ValueError('Storage/writer inventory is invalid')

    def run(self, *args, input=None, output=None, check=True):
        result = subprocess.run(self.compose + list(args), input=input, stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if check and result.returncode:
            # Never put credentials or arbitrary provider/SQL error bodies in logs.
            raise RuntimeError('Compose operation failed: ' + args[0])
        return result

    def sql(self, sql):
        return self.run('exec', '-T', self.pg, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', self.user,
                        '-d', self.database, '-At', input=sql.encode()).stdout.decode().strip()

    def running(self):
        return self.run('ps', '--status', 'running', '--services').stdout.decode().split()

    def close_writers(self, phase):
        active = [s for s in self.running() if s in self.writers]
        self.gate.touch()
        atomic_json(self.marker, {'phase': phase, 'at': time.time(), 'project': self.project})
        self.run('stop', '-t', '100', *self.writers)
        if set(self.running()) & set(self.writers):
            raise RuntimeError('Writers did not stop')
        return active

    def s3(self, spec):
        import boto3
        from botocore.config import Config
        credentials = json.loads(Path(spec['credentials_file']).read_text()) if spec.get('credentials_file') else {}
        return boto3.Session(profile_name=spec.get('profile'), **credentials).client('s3', endpoint_url=spec.get('endpoint'),
            config=Config(connect_timeout=5, read_timeout=30, retries={'max_attempts': 2}, s3={'addressing_style': 'path'}))

    def state_metadata(self):
        result = {}
        for name in ('sql', 'config', 'objects'):
            if name == 'objects' and self.c.get('objects_s3'):
                continue
            directory = self.root/name
            for path in [directory, *directory.rglob('*')]:
                if path.is_symlink():
                    raise ValueError('State inventory cannot contain symlinks')
                info = path.stat()
                if not (path.is_file() or path.is_dir()):
                    raise ValueError('Unsupported state entry')
                result[str(path.relative_to(self.root))] = {'uid':info.st_uid,'gid':info.st_gid,'mode':stat.S_IMODE(info.st_mode)}
        return result

    @staticmethod
    def restore_metadata(staged, name, metadata):
        # The systemd operator is privileged to preserve the container UIDs.
        # A nonprivileged invocation fails closed instead of widening access.
        for relative, info in metadata.items():
            path = Path(relative)
            if path.parts[0] != name:
                continue
            target = staged.joinpath(*path.parts[1:])
            if not target.resolve().is_relative_to(staged.resolve()):
                raise ValueError('Invalid state metadata path')
            existing = target.stat()
            if (existing.st_uid,existing.st_gid) != (info['uid'],info['gid']):
                os.chown(target, info['uid'],info['gid'])
            os.chmod(target,info['mode'])

    def fetch_objects(self, destination):
        spec = self.c.get('objects_s3')
        if not spec:
            shutil.copytree(self.root/'objects', destination)
            return
        client = self.s3(spec)
        destination.mkdir()
        for page in client.get_paginator('list_objects_v2').paginate(Bucket=spec['bucket']):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if not re.fullmatch(r'[a-z0-9_-]+/[a-z0-9_-]+/(original|preview)', key):
                    raise ValueError('Attachment bucket must be dedicated to private attachment objects')
                path = destination/key; path.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(spec['bucket'], key, str(path))

    def business(self):
        return {table: json.loads(self.sql("SELECT json_build_object('count',count(*),'digest',md5(COALESCE(string_agg(row_to_json(t)::text,'' ORDER BY row_to_json(t)::text),''))) FROM " + table + " t;")) for table in BUSINESS_TABLES}

    @staticmethod
    def remote_digest(client, bucket, key):
        result = hashlib.sha256()
        with client.get_object(Bucket=bucket, Key=key)['Body'] as stream:
            for chunk in iter(lambda: stream.read(CHUNK), b''):
                result.update(chunk)
        return result.hexdigest()

    def verify_target_objects(self, objects):
        spec = self.c.get('objects_s3')
        client = self.s3(spec) if spec else None
        for obj in objects:
            for key, expected in [(obj['key'], obj['sha256']), (obj['preview'], obj['preview_sha256'])]:
                actual = self.remote_digest(client, spec['bucket'], key) if spec else digest(self.root/'objects'/key)
                if actual != expected:
                    raise ValueError('Restored target object hash mismatch')

    def backup(self):
        if self.marker.exists():
            raise RuntimeError('Maintenance is already active; finish or investigate it first')
        active = self.close_writers('backup')
        started = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix='aihd-backup-') as temp:
                root = Path(temp)
                ownership = self.state_metadata()
                with (root/'postgres.dump').open('wb') as output:
                    self.run('exec', '-T', self.pg, 'pg_dump', '-U', self.user, '-d', self.database, '-Fc', output=output)
                for name in ('sql', 'config'):
                    shutil.copytree(self.root/name, root/name)
                deployment = root/'config'/'deployment'; deployment.mkdir(exist_ok=True)
                names = set()
                for file in self.c.get('config_files', [self.c['env_file']]):
                    source = Path(file)
                    if source.resolve() == Path(self.c['key_file']).resolve() or source.is_symlink() or source.name in names:
                        raise ValueError('Invalid deployment configuration inventory')
                    names.add(source.name); shutil.copy2(source, deployment/source.name)
                self.fetch_objects(root/'objects')
                # Private objects are validated against their persisted identities.
                objects = json.loads(self.sql("SELECT COALESCE(json_agg(json_build_object('key',object_key,'sha256',sha256,'preview',preview_key,'preview_sha256',preview_sha256)),'[]') FROM attachments WHERE state='ready';"))
                for obj in objects:
                    if digest(root/'objects'/obj['key']) != obj['sha256'] or digest(root/'objects'/obj['preview']) != obj['preview_sha256']:
                        raise ValueError('Attachment hash mismatch')
                self.check_sqlite(root/'sql')
                files = {str(p.relative_to(root)): {'sha256': digest(p), 'bytes': p.stat().st_size} for p in root.rglob('*') if p.is_file()}
                manifest = {'format': 1, 'snapshot_at': started, 'project': self.project, 'files': files,
                            'objects': objects, 'ownership':ownership, 'application': json.loads((self.output/'active-release.json').read_text()) if (self.output/'active-release.json').exists() else self.c.get('release', {}), 'business': self.business(), 'consistency': 'all project writers stopped'}
                (root/'manifest.json').write_text(json.dumps(manifest))
                archive = root/'snapshot.tar'
                with tarfile.open(archive, 'w') as tar:
                    for name in ['postgres.dump', 'sql', 'config', 'objects', 'manifest.json']:
                        tar.add(root/name, arcname=name, recursive=True)
                destination = self.output/(time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(started)) + '-' + os.urandom(4).hex() + '.aihdbak')
                crypt(archive, str(destination)+'.partial', self.c['key_file'])
                os.replace(str(destination)+'.partial', destination)
                offsite = self.c.get('offsite')
                if offsite:
                    self.s3(offsite).upload_file(str(destination), offsite['bucket'], offsite.get('prefix', '') + destination.name)
                    remote = self.s3(offsite).head_object(Bucket=offsite['bucket'], Key=offsite.get('prefix', '') + destination.name)
                    if remote['ContentLength'] != destination.stat().st_size or self.remote_digest(self.s3(offsite), offsite['bucket'], offsite.get('prefix', '') + destination.name) != digest(destination):
                        raise ValueError('Offsite size verification failed')
                elif self.c.get('require_offsite'):
                    raise ValueError('Offsite backup is required')
                report = {'artifact': str(destination), 'snapshot_at': started, 'completed_at': time.time(),
                          'bytes': destination.stat().st_size, 'files': len(files), 'objects': len(objects),
                          'offsite': bool(offsite), 'sha256': digest(destination), 'business':manifest['business']}
                atomic_json(self.output/'last-backup.json', report)
                atomic_json(str(destination)+'.json', report)
                self.metric('backup', report['completed_at'])
                publish_metric_status(self.c,'last-backup.json','completed_at',report['completed_at'])
                if self.c.get('retention', {}).get('apply_after_backup'):
                    report['retention'] = self.prune(apply=True)
                return report
        finally:
            # Backup never mutates the source. Restart only services previously up.
            if active:
                self.run('start', *active)
            self.marker.unlink(missing_ok=True)
            self.gate.unlink(missing_ok=True)

    def prune(self, *, apply=False):
        policy = self.c.get('retention', {})
        days, keep = int(policy.get('days',30)), int(policy.get('keep_last',48))
        if days < 7 or keep < 2:
            raise ValueError('Retention requires at least 7 days and 2 newest snapshots')
        verified_path = self.output/'last-verification.json'
        if not verified_path.exists():
            return {'deleted':[], 'candidates':[], 'reason':'No verified recovery snapshot; keep every backup'}
        verified = json.loads(verified_path.read_text())['artifact_sha256']
        newest = json.loads((self.output/'last-backup.json').read_text())['sha256']
        snapshots = []
        for file in self.output.glob('*.aihdbak'):
            sidecar = Path(str(file)+'.json')
            if file.is_symlink() or not sidecar.is_file() or sidecar.is_symlink():
                continue  # Never delete unrecognized/operator-managed artifacts.
            info = json.loads(sidecar.read_text())
            if Path(info['artifact']).resolve() != file.resolve() or digest(file) != info['sha256']:
                raise ValueError('Backup catalog changed; retention stopped')
            snapshots.append((file,info))
        known = {info['sha256'] for _,info in snapshots}
        if not {verified,newest}.issubset(known):
            raise ValueError('Newest or verified snapshot missing; retention stopped')
        snapshots.sort(key=lambda row:row[1]['snapshot_at'],reverse=True)
        protected = {verified,newest} | {info['sha256'] for _,info in snapshots[:keep]}
        cutoff = time.time()-days*86400
        candidates = [file for file,info in snapshots if info['snapshot_at'] < cutoff and info['sha256'] not in protected]
        if apply:
            for file in candidates:
                file.unlink();Path(str(file)+'.json').unlink()
        result = {'candidates':[file.name for file in candidates], 'deleted':[file.name for file in candidates] if apply else [],
                  'protected_sha256':sorted(protected), 'days':days, 'keep_last':keep, 'remote_deleted':False}
        atomic_json(self.output/'last-retention.json',result)
        return result

    @staticmethod
    def check_sqlite(root):
        for path in root.rglob('*'):
            if path.is_file() and path.suffix in {'.db', '.sqlite', '.sqlite3'}:
                with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('SQL state integrity check failed')

    def unpack(self, artifact, destination):
        archive = destination/'snapshot.tar'
        crypt(artifact, archive, self.c['key_file'], decrypt=True)
        with tarfile.open(archive) as tar:
            for item in tar.getmembers():
                if item.issym() or item.islnk() or not (destination/item.name).resolve().is_relative_to(destination.resolve()):
                    raise ValueError('Unsafe snapshot path')
                if not (item.isfile() or item.isdir()):
                    raise ValueError('Unsafe snapshot entry')
            tar.extractall(destination, filter='data')
        manifest = json.loads((destination/'manifest.json').read_text())
        if manifest.get('format') != 1:
            raise ValueError('Unsupported snapshot format')
        for name, info in manifest['files'].items():
            if digest(destination/name) != info['sha256']:
                raise ValueError('Snapshot manifest verification failed')
        self.check_sqlite(destination/'sql')
        return manifest

    def restore(self, artifact):
        self.close_writers('restore_in_progress')
        started = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix='aihd-restore-') as temp:
                root = Path(temp)
                manifest = self.unpack(artifact, root)  # All authentication/hash checks before mutation.
                self.run('exec', '-T', self.pg, 'pg_restore', '-U', self.user, '-d', self.database,
                         '--clean', '--if-exists', '--no-owner', '--exit-on-error', input=(root/'postgres.dump').read_bytes())
                # Old account state is not an authority. Disable everybody until
                # reviewed current roles are supplied by the deployment operator.
                self.sql("BEGIN; UPDATE staff_accounts SET active=false,auth_version=auth_version+1; DELETE FROM staff_sessions; COMMIT;")
                for name in ('sql', 'config', 'objects'):
                    if name == 'objects' and self.c.get('objects_s3'):
                        spec = self.c['objects_s3']; client = self.s3(spec)
                        for item in (root/name).rglob('*'):
                            if item.is_file():
                                client.upload_file(str(item), spec['bucket'], str(item.relative_to(root/name)))
                        continue
                    target = self.root/name
                    staged = self.root/(name+'.restore-'+os.urandom(4).hex())
                    shutil.copytree(root/name, staged)
                    self.restore_metadata(staged, name, manifest.get('ownership',{}))
                    if target.exists():
                        shutil.rmtree(target)
                    os.replace(staged, target)
                self.verify_target_objects(manifest['objects'])
                self.check_sqlite(self.root/'sql')
                if self.business() != manifest['business']:
                    raise ValueError('Restored business rows differ from the snapshot')
                report = {'business': manifest['business'], 'restored_at': time.time(), 'snapshot_at': manifest['snapshot_at'], 'restore_seconds': time.time()-started,
                          'artifact': str(artifact), 'phase': 'awaiting_access_review_and_business_smoke', 'writers_started': False}
                atomic_json(self.marker, report)
                atomic_json(self.output/'last-restore.json', report)
                return report
        except BaseException:
            atomic_json(self.marker, {'phase': 'restore_failed_writers_closed', 'at': time.time(), 'writers_started': False})
            raise

    def review_access(self, file):
        state = json.loads(self.marker.read_text())
        if state['phase'] != 'awaiting_access_review_and_business_smoke':
            raise ValueError('No completed restore awaiting review')
        approved = json.loads(Path(file).read_text())
        if not approved or not any(x.get('role') == 'admin' and x.get('active') for x in approved):
            raise ValueError('Reviewed administrator required')
        known = set(json.loads(self.sql("SELECT COALESCE(json_agg(actor_id),'[]') FROM staff_accounts")))
        if any(row.get('actor_id') not in known for row in approved):
            raise ValueError('Review names an unknown employee')
        # IDs and enum values only, no SQL from the review file.
        queries = ["BEGIN", "UPDATE staff_accounts SET active=false,auth_version=auth_version+1", "DELETE FROM staff_sessions"]
        for row in approved:
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', row['actor_id']) or row['role'] not in {'employee','operations','admin'} or type(row['active']) is not bool:
                raise ValueError('Invalid access review')
            queries.append("UPDATE staff_accounts SET role='%s',active=%s WHERE actor_id='%s'" % (row['role'], str(row['active']).lower(), row['actor_id']))
        queries += ['COMMIT']
        self.sql(';'.join(queries)+';')
        state['phase'] = 'awaiting_business_smoke'; state['access_review_sha256'] = digest(file)
        atomic_json(self.marker, state)
        return state

    def metric(self, operation, when):
        path = self.output/(operation+'.prom')
        path.write_text(f'aihd_{operation}_last_success_timestamp_seconds {when}\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--allow-production', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Apply a reviewed local retention plan')
    parser.add_argument('operation', choices=['backup','restore','review-access','prune'])
    parser.add_argument('input', nargs='?')
    args = parser.parse_args()
    ops = Operations(args.config, args.allow_production)
    if args.operation == 'backup': result = ops.backup()
    elif args.operation == 'restore': result = ops.restore(args.input)
    elif args.operation == 'prune': result = ops.prune(apply=args.apply)
    else: result = ops.review_access(args.input)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
