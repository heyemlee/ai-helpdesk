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
    boundary = 0 if args.baseline else 1
    with tempfile.TemporaryDirectory(prefix='aihd-release-build-') as folder:
        root = Path(folder)
        archive = root/'source.tar'
        archive.write_bytes(subprocess.check_output(['git', 'archive', revision]))
        with tarfile.open(archive) as tar:
            tar.extractall(root, filter='data')
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
        Path(args.output).write_text(json.dumps({'format':1,'revision':revision,'staff_boundary':boundary,'contract':'1.14',
                                                'images':images}, indent=2))


if __name__ == '__main__': main()
