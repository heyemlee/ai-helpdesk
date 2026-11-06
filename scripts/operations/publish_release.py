#!/usr/bin/env python3
"""Publish the exact tested images and emit registry-digest deployment input."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from acceptance.gate import require as require_acceptance

from recovery import atomic_json, digest
from release import validated_manifest


def verification(manifest, reports):
    if set(manifest.get('images',{})) != {'backend','frontend','sql'}:
        raise ValueError('Exactly three application image identities are required')
    checked=[]
    backends=set()
    for file in reports:
        result=json.loads(Path(file).read_text())
        if result.get('failed_with') or not result.get('completed_at'):
            raise ValueError('Incomplete recovery verification')
        if result.get('tested_image_ids') != manifest['images']:
            raise ValueError('Verification belongs to different image artifacts')
        for name in ['resource_isolation_verified','public_ingress_auth_verified','maintenance_public_port_closed_verified',
                     'nonroot_api_reads_backup_verification_metrics','corrupt_snapshot_fail_closed',
                     'partial_restore_fail_closed_after_pg_commit','sessions_revoked_and_accounts_closed',
                     'rotated_sql_key_fail_closed_through_employee_gateway',
                     'failed_release_rolled_back_to_safe_images','persisted_release_matches_running']:
            if result.get(name) is not True:
                raise ValueError('Required recovery boundary was not verified: '+name)
        for name in ['initial_release','reopened_release']:
            stage=result[name]
            if stage.get('images') != manifest['images']:
                raise ValueError('Business smoke belongs to different image artifacts')
            if stage.get('sql_gateway',{}).get('passed') is not True:
                raise ValueError('Real employee SQL gateway was not verified')
            if stage['revision'] != manifest['revision'] or stage['checks'].get('passed') is not True or not stage['checks'].get('workers_closed_during_business_smoke') or stage['checks'].get('attachment_downloads_verified',0) < 2:
                raise ValueError('Release was not verified with employee and attachment access')
        backends.add(result['object_backend'])
        if result['object_backend']=='s3-compatible-local-fixture' and not result.get('actual_s3_target_corruption_fail_closed'):
            raise ValueError('Actual S3 target hash check was not verified')
        checked.append({'sha256':digest(file),'completed_at':result['completed_at']})
    if backends != {'local-private-files','s3-compatible-local-fixture'}:
        raise ValueError('Both local and S3 recovery verification are required')
    return checked


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--report',action='append',required=True)
    parser.add_argument('--acceptance',required=True,help='Unified stage-6 report and its content-addressed evidence directory')
    parser.add_argument('--registry',required=True,help='Lowercase registry/repository prefix')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9./_-]+',args.registry):
        raise ValueError('Invalid registry/repository prefix')
    manifest=validated_manifest(args.manifest)
    checked=verification(manifest,args.report)
    acceptance=require_acceptance(args.acceptance,manifest)
    published={}
    for service,image in manifest['images'].items():
        repo=args.registry+'/'+service
        tag=repo+':'+manifest['revision']
        subprocess.run(['docker','tag',image,tag],check=True)
        subprocess.run(['docker','push',tag],check=True)
        info=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]
        if info['Id'] != json.loads(subprocess.check_output(['docker','image','inspect',image]))[0]['Id']:
            raise ValueError('Published image differs from the tested image')
        published[service]=next(ref for ref in info['RepoDigests'] if ref.startswith(repo+'@sha256:'))
    atomic_json(args.output,{**manifest,'images':published,'tested_image_ids':manifest['images'],'verification':checked,'acceptance':acceptance})


if __name__=='__main__':main()
