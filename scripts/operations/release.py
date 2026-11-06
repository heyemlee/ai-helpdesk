#!/usr/bin/env python3
"""Apply immutable images behind a closed ingress; fail back only to a tested safe release."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from recovery import Operations, atomic_json, digest


def artifact_identity(manifest, inspect_image=None):
    """Registry references and local IDs must resolve to the same tested bytes."""
    if inspect_image is None:
        inspect_image = lambda image: json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]
    if manifest.get('staff_boundary') != 1 or manifest.get('contract') not in ('1.14', '1.15'):
        raise ValueError('Release lacks the minimum employee/contract boundary; keep maintenance closed')
    if set(manifest.get('images',{})) != {'backend','frontend','sql'}:
        raise ValueError('Exactly three application image identities are required')
    identities = {}
    tested = manifest.get('tested_image_ids')
    if tested is not None and set(tested) != {'backend','frontend','sql'}:
        raise ValueError('Published manifest lacks complete tested image identities')
    for service, image in manifest['images'].items():
        if not re.fullmatch(r'(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}', image):
            raise ValueError('Immutable image ID or registry digest required')
        info = inspect_image(image)
        labels = info['Config'].get('Labels') or {}
        if labels.get('ai-helpdesk.staff-boundary') != '1' or labels.get('org.opencontainers.image.revision') != manifest['revision']:
            raise ValueError('Image does not match the release manifest')
        identity = info['Id']
        if tested is not None and identity != tested[service]:
            raise ValueError('Registry digest resolves to a different tested image')
        if image.startswith('sha256:') and identity != image:
            raise ValueError('Local immutable identity changed')
        if '@sha256:' in image and image not in info.get('RepoDigests', []):
            raise ValueError('Registry digest is not bound to the inspected image')
        identities[service] = identity
    return {'revision': manifest['revision'], 'images': identities}


def validated_manifest(file):
    manifest = json.loads(Path(file).read_text())
    artifact_identity(manifest)
    return manifest


def apply(ops, manifest, probe):
    source = Path(ops.c['env_file']).read_text().splitlines()
    images = {'BACKEND_IMAGE':manifest['images']['backend'], 'SQL_IMAGE':manifest['images']['sql'], 'FRONTEND_IMAGE':manifest['images']['frontend']}
    lines = [line for line in source if line.split('=',1)[0] not in images]
    lines += [k+'='+v for k,v in images.items()]
    candidate = ops.output/'candidate.env'; candidate.write_text('\n'.join(lines)+'\n'); candidate.chmod(0o600)
    ops.compose[ops.compose.index('--env-file')+1] = str(candidate)
    ops.run('up','-d','--no-deps','--no-build','--pull','never','api','frontend')
    if {'worker','sql-agent'} & set(ops.running()):
        raise RuntimeError('Writers started before employee/business verification')
    # Container health alone never reopens access.
    for _ in range(60):
        check = ops.run('exec','-T','api','curl','-fsS','http://localhost:8000/readyz',check=False)
        if check.returncode == 0:
            break
        time.sleep(1)
    result = ops.run('exec','-T','api','python','scripts/release_smoke.py', input=Path(probe).read_bytes())
    if not json.loads(result.stdout)['passed']:
        raise ValueError('Employee/business smoke failed')
    checks=json.loads(result.stdout)
    checks['workers_closed_during_business_smoke']=True
    return checks


def activate(ops, probe):
    # Validation has passed. Persist the exact image configuration before reopen.
    candidate = ops.output/'candidate.env'
    destination = Path(ops.c['env_file'])
    staged = destination.with_suffix('.next')
    staged.write_bytes(candidate.read_bytes()); staged.chmod(0o600)
    os.replace(staged, destination)
    ops.compose[ops.compose.index('--env-file')+1] = str(destination)
    for service in ['sql-agent', 'worker']:
        ops.run('up','-d','--no-deps','--no-build','--pull','never',service)
        for attempt in range(60):
            check = ops.run('exec','-T',service,'python','-c',
                "import urllib.request;urllib.request.urlopen('http://localhost:8000/readyz',timeout=2)",check=False)
            if check.returncode == 0: break
            if attempt == 59: raise RuntimeError('Worker/data service readiness failed')
            time.sleep(1)
        if service == 'sql-agent':
            result=ops.run('exec','-T','api','python','scripts/release_smoke.py','--sql',input=Path(probe).read_bytes())
            gateway=json.loads(result.stdout)
            atomic_json(ops.output/'last-sql-gateway-smoke.json',gateway)
            if not gateway['passed']:
                raise ValueError('Employee SQL gateway smoke failed')
            ops.run('exec','-T','api','python','-c',
                "import os,httpx; r=httpx.get('http://sql-agent:8000/whoami',headers={'X-API-Key':os.environ['SQL_AGENT_API_KEY']});r.raise_for_status();i=r.json()['identity'];assert i['user_id']==os.getenv('SQL_AGENT_USER_ID','ai-helpdesk') and not i['is_admin'] and not i.get('can_impersonate',False)")


def public_auth_status(origin, timeout):
    """Probe the actual origin without redirects, proxies or employee credentials."""
    address = urlsplit(origin)
    if (address.scheme not in {'http', 'https'} or not address.hostname
            or address.username is not None or address.password is not None
            or address.path not in {'', '/'} or address.query or address.fragment):
        raise ValueError('An anonymous public HTTP(S) origin is required')
    transport = http.client.HTTPSConnection if address.scheme == 'https' else http.client.HTTPConnection
    connection = transport(address.hostname, address.port, timeout=timeout)
    try:
        connection.request('GET', '/api/auth/session', headers={'Accept':'application/json'})
        response = connection.getresponse()
        try:
            return response.status
        finally:
            response.close()
    finally:
        connection.close()


def wait_public_auth(origin, timeout=30):
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Public ingress authentication readiness timed out') from last_error
        try:
            status = public_auth_status(origin, min(2, remaining))
        except (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError, http.client.RemoteDisconnected) as error:
            last_error = error
        except socket.gaierror as error:
            if error.errno != socket.EAI_AGAIN:
                raise
            last_error = error
        else:
            if time.monotonic() >= deadline:
                raise TimeoutError('Public ingress authentication readiness timed out')
            if status == 401:
                return
            if status not in {502, 503, 504}:
                raise ValueError(f'Public ingress authentication check failed (HTTP {status})')
            last_error = RuntimeError(f'Public ingress not ready (HTTP {status})')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Public ingress authentication readiness timed out') from last_error
        time.sleep(min(.25, remaining))


def reopen_ingress(ops, probe):
    origin = json.loads(Path(probe).read_text())['origin']
    ops.run('up', '-d', '--no-build', 'ingress')
    # The gate must be lifted to exercise authentication through the real route.
    # Keep the operation marker until that check succeeds; callers close on error.
    ops.gate.unlink(missing_ok=True)
    wait_public_auth(origin)
    ops.marker.unlink(missing_ok=True)


def restore_file(path, contents, mode):
    if contents is None:
        path.unlink(missing_ok=True)
    elif (not path.exists() or path.read_bytes() != contents
          or path.stat().st_mode & 0o777 != mode):
        staged = path.with_name(path.name+'.restore')
        staged.write_bytes(contents); staged.chmod(mode)
        os.replace(staged, path)


def deploy(ops, target, probe):
    candidate = validated_manifest(target)  # Old unsafe images rejected before any switch.
    if ops.c.get('environment') != 'drill':
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from acceptance.gate import require as require_acceptance
        evidence = ops.c.get('acceptance_report')
        if not evidence:
            raise ValueError('Production release requires a complete unified acceptance bundle')
        require_acceptance(evidence, artifact_identity(candidate))
    existing_file = ops.output/'active-release.json'
    previous = json.loads(existing_file.read_text()) if existing_file.exists() else None
    if ops.marker.exists():
        state = json.loads(ops.marker.read_text())
        if state['phase'] != 'awaiting_business_smoke':
            raise ValueError('Recovery is not complete or access review is missing')
    environment = Path(ops.c['env_file'])
    last_release = ops.output/'last-release.json'
    original_files = [(path, path.read_bytes() if path.exists() else None,
                       path.stat().st_mode & 0o777 if path.exists() else 0o600)
                      for path in (environment, existing_file, last_release)]

    def fail_closed(phase):
        try:
            ops.close_writers(phase)
        finally:
            ops.compose[ops.compose.index('--env-file')+1] = str(environment)
            for path, contents, mode in original_files:
                restore_file(path, contents, mode)

    ops.close_writers('release_validation')
    try:
        checks = apply(ops, candidate, probe)
        activate(ops,probe)
        reopen_ingress(ops,probe)
        atomic_json(existing_file,candidate)
        atomic_json(last_release,{'outcome':'validated','revision':candidate['revision'],'manifest_sha256':digest(target),'checks':checks,'at':time.time()})
        ops.metric('release',time.time())
        return {'outcome':'validated','revision':candidate['revision'],'images':candidate['images'],'checks':checks,
                'sql_gateway':json.loads((ops.output/'last-sql-gateway-smoke.json').read_text())}
    except BaseException as candidate_error:
        if previous:
            try:
                fail_closed('rollback_validation')
                rollback = ops.output/'rollback.json'; atomic_json(rollback,previous)
                safe = validated_manifest(rollback)
                apply(ops,safe,probe)
                activate(ops,probe)
                reopen_ingress(ops,probe)
                atomic_json(existing_file,safe)
                atomic_json(last_release,{'outcome':'rolled_back','revision':safe['revision'],'at':time.time()})
            except BaseException as rollback_error:
                fail_closed('rollback_failed_writers_closed')
                raise candidate_error from rollback_error
        else:
            fail_closed('release_failed_writers_closed')
        raise


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--manifest',required=True);p.add_argument('--probe',required=True);p.add_argument('--allow-production',action='store_true')
    args=p.parse_args()
    print(json.dumps(deploy(Operations(args.config,args.allow_production),args.manifest,args.probe)))


if __name__ == '__main__': main()
