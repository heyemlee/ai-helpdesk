#!/usr/bin/env python3
"""Build once from a clean committed archive; stage and publish these exact IDs."""
import argparse
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile


def run(*args):
    return subprocess.check_output(args).decode().strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ref', default='HEAD')
    p.add_argument('--output', required=True)
    p.add_argument('--baseline', action='store_true', help='Build the isolated pre-Case comparison; never release eligible')
    args = p.parse_args()
    revision = run('git', 'rev-parse', args.ref+'^{commit}')
    if args.baseline and not revision.startswith('5310da9'):
        raise ValueError('The comparison baseline is fixed at 5310da9')
    if not args.baseline and subprocess.run(['git','merge-base','--is-ancestor','34b4298f3e1966d71c7ed92d4c1bdabb06727291',revision],capture_output=True).returncode:
        raise ValueError('Release source must descend from the accepted staff-authentication boundary; old sources require the explicit baseline path')
    boundary = 0 if args.baseline else 1
    with tempfile.TemporaryDirectory(prefix='aihd-release-build-') as folder:
        root = Path(folder)
        archive = root/'source.tar'
        archive.write_bytes(subprocess.check_output(['git', 'archive', revision]))
        with tarfile.open(archive) as tar:
            tar.extractall(root, filter='data')
        contract=json.loads((root/'a2a-contract/fixtures/index.json').read_text())['contract_version'].removeprefix('v')
        if not args.baseline and not (root/'backend/app/core/staff_auth.py').is_file():
            raise ValueError('Staff authentication implementation is absent from the archived source')
        images = {}
        for service, context, dockerfile, arg in [
            ('backend','backend','Dockerfile','AI_HELPDESK_RELEASE'),
            ('frontend','frontend','Dockerfile','AI_HELPDESK_RELEASE'),
            ('sql','ai-sql-agent','deploy/Dockerfile','AI_SQL_RELEASE')]:
            tag = f'aihd-release/{service}:{revision}'
            subprocess.run(['docker','build','-t',tag,'-f',str(root/context/dockerfile),
                            *(['--target','production'] if service == 'backend' else []), '--build-arg',f'{arg}={revision}', '--label',f'org.opencontainers.image.revision={revision}',
                            '--label',f'ai-helpdesk.staff-boundary={boundary}',str(root/context)], check=True)
            images[service] = run('docker','image','inspect','--format','{{.Id}}',tag)
        Path(args.output).write_text(json.dumps({'format':1,'revision':revision,'staff_boundary':boundary,'contract':contract,
                                                'images':images}, indent=2))


if __name__ == '__main__': main()
