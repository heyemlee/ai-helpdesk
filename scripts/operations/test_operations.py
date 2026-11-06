"""Failure boundaries for encrypted snapshots and conservative local retention."""
import base64
import http.client
import json
import os
from pathlib import Path
import ssl
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from recovery import Operations, atomic_json, crypt, digest, publish_metric_status
from publish_release import verification
from verify import _verify
from release import artifact_identity, deploy


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihd-ops-test-')
        self.root = Path(self.temp.name)
        self.key = self.root/'key'
        self.key.write_text(base64.b64encode(os.urandom(32)).decode())
        self.key.chmod(0o600)
        self.source = self.root/'source'
        self.source.write_bytes(os.urandom(2*1024*1024+17))
        self.archive = self.root/'snapshot.aihdbak'
        crypt(self.source,self.archive,self.key)

    def tearDown(self):
        self.temp.cleanup()

    def test_authenticated_stream_roundtrip_and_ciphertext_corruption(self):
        restored = self.root/'restored'
        crypt(self.archive,restored,self.key,decrypt=True)
        self.assertEqual(digest(self.source),digest(restored))
        payload = bytearray(self.archive.read_bytes());payload[80] ^= 1
        self.archive.write_bytes(payload)
        with self.assertRaises(Exception):
            crypt(self.archive,restored,self.key,decrypt=True)

    def test_wrong_key_and_truncated_header_fail(self):
        self.key.write_text(base64.b64encode(os.urandom(32)).decode())
        with self.assertRaises(Exception):
            crypt(self.archive,self.root/'restored',self.key,decrypt=True)
        self.archive.write_bytes(b'AIHDBAK1'+b'0'*15)
        with self.assertRaisesRegex(ValueError,'Truncated'):
            crypt(self.archive,self.root/'restored',self.key,decrypt=True)

    def test_key_with_broad_permissions_is_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'0600'):
            crypt(self.source,self.root/'bad',self.key)

    def test_verification_rejects_another_valid_snapshot_at_the_registered_path(self):
        expected=digest(self.archive)
        self.source.write_bytes(b'another legitimately encrypted old snapshot')
        crypt(self.source,self.archive,self.key)
        atomic_json(self.root/'last-backup.json',{'artifact':str(self.archive),'sha256':expected,'snapshot_at':123})
        previous={'verified_at':12,'artifact_sha256':'previous-verified'}
        atomic_json(self.root/'last-verification.json',previous)
        with patch('verify.Operations') as operations:
            with self.assertRaisesRegex(ValueError,'ciphertext digest'):
                _verify({'backup_dir':str(self.root)})
            operations.assert_not_called()
        self.assertEqual(json.loads((self.root/'last-verification.json').read_text()),previous)

    def test_manifest_time_mismatch_does_not_refresh_verification_success(self):
        atomic_json(self.root/'last-backup.json',{'artifact':str(self.archive),'sha256':digest(self.archive),'snapshot_at':123})
        previous={'verified_at':12,'artifact_sha256':'previous-verified'}
        atomic_json(self.root/'last-verification.json',previous)
        with patch('verify.Operations') as operations:
            operations.return_value.restore.return_value={'snapshot_at':122,'business':{}}
            with self.assertRaisesRegex(ValueError,'snapshot time'):
                _verify({'backup_dir':str(self.root),'key_file':str(self.key)})
        self.assertEqual(json.loads((self.root/'last-verification.json').read_text()),previous)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aihd-retention-test-')
        self.root = Path(self.temp.name)
        self.ops = Operations.__new__(Operations)
        self.ops.output = self.root
        self.ops.c = {'retention':{'days':7,'keep_last':2}}
        self.snapshots = []
        for age in [40,30,20,10,1]:
            path=self.root/f'age-{age}.aihdbak';path.write_bytes(os.urandom(64))
            info={'artifact':str(path),'sha256':digest(path),'snapshot_at':time.time()-age*86400}
            atomic_json(str(path)+'.json',info)
            self.snapshots.append((path,info))
        atomic_json(self.root/'last-backup.json',self.snapshots[-1][1])
        atomic_json(self.root/'last-verification.json',{'artifact_sha256':self.snapshots[0][1]['sha256']})

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_and_delete_keep_latest_two_and_last_verified(self):
        planned=self.ops.prune()
        self.assertEqual(set(planned['candidates']),{'age-30.aihdbak','age-20.aihdbak'})
        self.assertEqual(planned['deleted'],[])
        self.assertTrue(all(path.exists() for path,_ in self.snapshots))
        applied=self.ops.prune(apply=True)
        self.assertEqual(applied['deleted'],planned['candidates'])
        self.assertEqual({p.name for p in self.root.glob('*.aihdbak')},{'age-40.aihdbak','age-10.aihdbak','age-1.aihdbak'})

    def test_never_delete_without_verified_recovery_or_known_catalog(self):
        (self.root/'last-verification.json').unlink()
        self.assertEqual(self.ops.prune(apply=True)['deleted'],[])
        self.assertTrue(all(path.exists() for path,_ in self.snapshots))

    def test_missing_verified_snapshot_blocks_cleanup(self):
        atomic_json(self.root/'last-verification.json',{'artifact_sha256':'missing'})
        with self.assertRaisesRegex(ValueError,'missing'):
            self.ops.prune(apply=True)
        self.assertTrue(all(path.exists() for path,_ in self.snapshots))

    def test_changed_catalog_or_ciphertext_blocks_cleanup(self):
        self.snapshots[1][0].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'catalog changed'):
            self.ops.prune(apply=True)
        self.assertTrue(all(path.exists() for path,_ in self.snapshots))

    def test_unknown_and_symlink_artifacts_are_never_deleted(self):
        unknown=self.root/'unknown.aihdbak';unknown.write_bytes(b'operator-owned')
        link=self.root/'linked.aihdbak';link.symlink_to(unknown)
        self.ops.prune(apply=True)
        self.assertTrue(unknown.exists() and link.is_symlink())


class ReleaseFlowOperations(Operations):
    """Run the release control flow against temporary files, without Docker."""
    def __init__(self, root, environment):
        self.output = root/'backups'; self.output.mkdir()
        self.env = root/'release.env'; self.env.write_text(environment); self.env.chmod(0o600)
        self.c = {'environment':'drill', 'env_file':str(self.env)}
        self.compose = ['docker', 'compose', '--env-file', str(self.env)]
        self.project = 'unit-release'
        self.gate = root/'gate/maintenance'; self.gate.parent.mkdir()
        self.marker = self.output/'maintenance.json'
        self.writers = ['api','frontend','sql-agent','worker','ingress']
        self.services = set()
        self.current_backend = None
        self.business_reject = None
        self.sql_reject = None
        self.events = []

    def running(self):
        return sorted(self.services)

    def run(self, *args, input=None, check=True):
        if args[0] == 'up':
            services = set(args) & set(self.writers)
            self.services.update(services)
            if 'api' in services:
                file = Path(self.compose[self.compose.index('--env-file')+1])
                self.current_backend = dict(line.split('=',1) for line in file.read_text().splitlines())['BACKEND_IMAGE']
            self.events.append(('up', sorted(services)))
            return SimpleNamespace(returncode=0, stdout=b'')
        if args[0] == 'stop':
            self.services.difference_update(set(args) & set(self.writers))
            self.events.append(('stop', sorted(self.services)))
            return SimpleNamespace(returncode=0, stdout=b'')
        if args[0] == 'exec':
            if 'scripts/release_smoke.py' in args:
                sql = '--sql' in args
                rejected = self.sql_reject if sql else self.business_reject
                result = {'passed':self.current_backend != rejected}
                if sql: result['status_code'] = 502 if not result['passed'] else 200
                else: result['attachment_downloads_verified'] = 2
                return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())
            return SimpleNamespace(returncode=0, stdout=b'')
        raise AssertionError('Unexpected operations call: '+repr(args))

    def metric(self, operation, when):
        self.events.append(('metric', operation))
        super().metric(operation, when)


class ReleaseFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aihd-release-flow-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.candidate = self.manifest('candidate', 1)
        self.previous = self.manifest('safe', 4)
        self.old_environment = 'KEEP=operator-setting\n'+self.image_environment(self.previous)
        self.ops = ReleaseFlowOperations(self.root, self.old_environment)
        self.target = self.root/'candidate.json'; atomic_json(self.target,self.candidate)
        self.probe = self.root/'probe.json'; atomic_json(self.probe,{'origin':'http://127.0.0.1:8765'})
        self.clock = 0.0
        self.attempts = []
        self.success_writes = []
        self.seen_public_401 = False
        self.response = lambda: 401
        self.fail_record = None
        for target, replacement in (
            ('release.subprocess.check_output', self.inspect),
            ('http.client.HTTPConnection', self.connection),
            ('http.client.HTTPSConnection', self.connection),
            ('release.time.monotonic', lambda: self.clock),
            ('release.time.sleep', self.sleep),
            ('release.atomic_json', self.record),
        ):
            patcher = patch(target, side_effect=replacement)
            patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def manifest(revision, start):
        return {'revision':revision, 'staff_boundary':1, 'contract':'1.15',
                'images':{name:'sha256:'+str(index)*64 for index,name in enumerate(('backend','frontend','sql'),start)}}

    @staticmethod
    def image_environment(manifest):
        return ''.join(key+'='+manifest['images'][service]+'\n' for key,service in
                       [('BACKEND_IMAGE','backend'),('SQL_IMAGE','sql'),('FRONTEND_IMAGE','frontend')])

    def inspect(self, command):
        reference = command[-1]
        manifest = next(m for m in (self.candidate,self.previous) if reference in m['images'].values())
        return json.dumps([{'Id':reference,'Config':{'Labels':{'ai-helpdesk.staff-boundary':'1',
                            'org.opencontainers.image.revision':manifest['revision']}}}]).encode()

    def sleep(self, seconds):
        self.assertGreater(seconds,0)
        self.clock += seconds

    def connection(self, *args, **kwargs):
        test = self
        class Connection:
            def request(self, method, path, headers):
                test.assertEqual((method,path),('GET','/api/auth/session'))
                test.assertNotIn('Authorization',headers)
                test.assertNotIn('Cookie',headers)

            def getresponse(self):
                test.assertFalse(test.ops.gate.exists())
                test.assertTrue(test.ops.marker.exists())
                result = test.response()
                test.attempts.append((result,test.ops.current_backend,kwargs['timeout']))
                if isinstance(result,BaseException): raise result
                if result == 401: test.seen_public_401 = True
                return SimpleNamespace(status=result, close=lambda:None)

            def close(self):
                pass
        return Connection()

    def record(self, path, value):
        if Path(path).name == 'active-release.json' or value.get('outcome') in ('validated','rolled_back'):
            self.success_writes.append((Path(path).name,value,self.seen_public_401))
        if Path(path).name == self.fail_record:
            self.fail_record = None
            raise OSError('injected success record failure')
        atomic_json(path,value)

    def use_previous(self):
        atomic_json(self.ops.output/'active-release.json',self.previous)
        atomic_json(self.ops.output/'last-release.json',{'outcome':'validated','revision':'safe','at':1})
        self.ops.services.update(self.ops.writers)

    def assert_closed(self):
        self.assertTrue(self.ops.gate.exists())
        self.assertTrue(self.ops.marker.exists())
        self.assertEqual(self.ops.running(),[])
        self.assertEqual(self.ops.env.read_text(),self.old_environment)
        self.assertEqual(self.ops.compose[self.ops.compose.index('--env-file')+1],str(self.ops.env))
        self.assertEqual(self.ops.env.stat().st_mode&0o777,0o600)

    def test_normal_release_waits_for_delayed_public_401_before_success(self):
        effects = iter([ConnectionRefusedError(),http.client.RemoteDisconnected(),503,401])
        self.response = lambda: next(effects)
        result = deploy(self.ops,self.target,self.probe)
        self.assertEqual([x[0] for x in self.attempts][-2:],[503,401])
        self.assertEqual(len(self.attempts),4)
        self.assertEqual(result['outcome'],'validated')
        self.assertTrue(self.success_writes)
        self.assertTrue(all(after_401 for _,_,after_401 in self.success_writes))
        self.assertFalse(self.ops.gate.exists()); self.assertFalse(self.ops.marker.exists())
        self.assertEqual(json.loads((self.ops.output/'active-release.json').read_text()),self.candidate)
        self.assertIn(self.image_environment(self.candidate),self.ops.env.read_text())

    def test_persistent_unavailability_times_out_and_restores_environment(self):
        self.response = lambda: ConnectionRefusedError()
        with self.assertRaises(TimeoutError): deploy(self.ops,self.target,self.probe)
        self.assertGreater(self.clock,0); self.assertLessEqual(self.clock,30)
        self.assertTrue(self.attempts); self.assertEqual(self.success_writes,[])
        self.assertFalse((self.ops.output/'active-release.json').exists())
        self.assert_closed()

    def test_anonymous_200_is_rejected_immediately_and_kept_closed(self):
        self.response = lambda: 200
        with self.assertRaisesRegex(ValueError,'200'): deploy(self.ops,self.target,self.probe)
        self.assertEqual(len(self.attempts),1); self.assertEqual(self.clock,0)
        self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_other_auth_and_protocol_errors_are_not_retried(self):
        for error in (301,302,403,404,407,500,ssl.SSLCertVerificationError('invalid certificate'),http.client.BadStatusLine('invalid HTTP')):
            with self.subTest(error=error):
                self.ops.marker.unlink(missing_ok=True); self.ops.gate.unlink(missing_ok=True)
                self.attempts.clear(); self.response = lambda: error
                with self.assertRaises(Exception): deploy(self.ops,self.target,self.probe)
                self.assertEqual(len(self.attempts),1); self.assertEqual(self.clock,0)
                self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_gateway_unavailability_is_bounded(self):
        self.response = lambda: 503
        with self.assertRaises(TimeoutError): deploy(self.ops,self.target,self.probe)
        self.assertGreater(self.clock,0); self.assertLessEqual(self.clock,30)
        self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_401_after_readiness_deadline_is_not_a_success(self):
        def late_response():
            self.clock += 31
            return 401
        self.response = late_response
        with self.assertRaises(TimeoutError): deploy(self.ops,self.target,self.probe)
        self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_rollback_waits_for_public_401_and_persists_safe_images(self):
        self.use_previous(); self.ops.business_reject = self.candidate['images']['backend']
        effects = iter([http.client.RemoteDisconnected(),502,401]); self.response = lambda: next(effects)
        with self.assertRaisesRegex(ValueError,'Employee/business smoke failed'):
            deploy(self.ops,self.target,self.probe)
        self.assertEqual(len(self.attempts),3)
        self.assertTrue(self.success_writes)
        self.assertTrue(all(after_401 for _,_,after_401 in self.success_writes))
        self.assertEqual(json.loads((self.ops.output/'last-release.json').read_text())['outcome'],'rolled_back')
        self.assertEqual(json.loads((self.ops.output/'active-release.json').read_text()),self.previous)
        self.assertEqual(self.ops.env.read_text(),self.old_environment)
        self.assertFalse(self.ops.gate.exists()); self.assertFalse(self.ops.marker.exists())

    def test_unavailable_rollback_keeps_previous_identity_but_closes_writers(self):
        self.use_previous(); self.ops.business_reject = self.candidate['images']['backend']
        self.response = lambda: http.client.RemoteDisconnected()
        with self.assertRaises(Exception): deploy(self.ops,self.target,self.probe)
        self.assertGreater(self.clock,0); self.assertEqual(self.success_writes,[])
        self.assertEqual(json.loads((self.ops.output/'active-release.json').read_text()),self.previous)
        self.assert_closed()

    def test_candidate_auth_failure_is_not_swallowed_by_safe_rollback(self):
        self.use_previous()
        self.response = lambda: 200 if self.ops.current_backend == self.candidate['images']['backend'] else 401
        with self.assertRaisesRegex(ValueError,'200'): deploy(self.ops,self.target,self.probe)
        self.assertEqual([x[0] for x in self.attempts],[200,401])
        self.assertEqual(json.loads((self.ops.output/'active-release.json').read_text()),self.previous)
        self.assertEqual(self.ops.env.read_text(),self.old_environment)
        self.assertTrue(all(after_401 for _,_,after_401 in self.success_writes))

    def test_candidate_timeout_can_roll_back_with_a_fresh_readiness_deadline(self):
        self.use_previous()
        effects = iter([504,401])
        self.response = lambda: (ConnectionRefusedError() if self.ops.current_backend == self.candidate['images']['backend']
                                 else next(effects))
        with self.assertRaises(TimeoutError): deploy(self.ops,self.target,self.probe)
        self.assertGreaterEqual(self.clock,30); self.assertLessEqual(self.clock,31)
        self.assertEqual(json.loads((self.ops.output/'last-release.json').read_text())['outcome'],'rolled_back')
        self.assertEqual(self.ops.env.read_text(),self.old_environment)
        self.assertEqual(json.loads((self.ops.output/'active-release.json').read_text()),self.previous)
        self.assertTrue(all(after_401 for _,_,after_401 in self.success_writes))

    def test_anonymous_200_during_rollback_is_fatal_and_preserves_the_cause(self):
        self.use_previous(); self.ops.business_reject = self.candidate['images']['backend']
        self.response = lambda: 200
        with self.assertRaisesRegex(ValueError,'Employee/business smoke failed') as failed:
            deploy(self.ops,self.target,self.probe)
        self.assertIsInstance(failed.exception.__cause__,ValueError)
        self.assertIn('200',str(failed.exception.__cause__))
        self.assertEqual(len(self.attempts),1); self.assertEqual(self.clock,0)
        self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_rollback_record_error_restores_prior_records_and_closes_writers(self):
        self.use_previous(); self.ops.business_reject = self.candidate['images']['backend']
        self.fail_record = 'last-release.json'
        with self.assertRaisesRegex(ValueError,'Employee/business smoke failed') as failed:
            deploy(self.ops,self.target,self.probe)
        self.assertIsInstance(failed.exception.__cause__,OSError)
        self.assertEqual(json.loads((self.ops.output/'last-release.json').read_text()),
                         {'outcome':'validated','revision':'safe','at':1})
        self.assertEqual(json.loads((self.ops.output/'active-release.json').read_text()),self.previous)
        self.assert_closed()

    def test_public_probe_refuses_origins_with_employee_credentials(self):
        atomic_json(self.probe,{'origin':'https://employee:secret@example.invalid'})
        with self.assertRaisesRegex(ValueError,'anonymous public'):
            deploy(self.ops,self.target,self.probe)
        self.assertEqual(self.attempts,[]); self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_failure_after_sql_activation_restores_environment_without_public_probe(self):
        self.ops.sql_reject = self.candidate['images']['backend']
        with self.assertRaisesRegex(ValueError,'Employee SQL gateway smoke failed'):
            deploy(self.ops,self.target,self.probe)
        self.assertEqual(self.attempts,[]); self.assertEqual(self.success_writes,[]); self.assert_closed()

    def test_success_record_error_does_not_leave_a_new_active_release(self):
        self.fail_record = 'last-release.json'
        with self.assertRaisesRegex(OSError,'injected success record failure'):
            deploy(self.ops,self.target,self.probe)
        self.assertTrue(self.seen_public_401)
        self.assertFalse((self.ops.output/'active-release.json').exists())
        self.assert_closed()


class PublicationTests(unittest.TestCase):
    def test_published_digest_resolves_to_exact_tested_images(self):
        ids = {name: 'sha256:' + str(index) * 64 for index, name in enumerate(['backend','frontend','sql'], 1)}
        registry = {name: 'example.invalid/app/' + name + '@sha256:' + str(index) * 64 for index, name in enumerate(ids, 4)}
        def inspected(reference):
            service = next(name for name in ids if reference in {ids[name],registry[name]})
            return {'Id':ids[service],'RepoDigests':[registry[service]],'Config':{'Labels':{'ai-helpdesk.staff-boundary':'1','org.opencontainers.image.revision':'candidate'}}}
        def changed(reference):
            info = inspected(reference)
            if reference == registry['backend']:info['Id']='sha256:'+'9'*64
            return info
        for contract in ('1.14', '1.15'):
            with self.subTest(contract=contract):
                manifest = {'revision':'candidate','staff_boundary':1,'contract':contract,'images':ids}
                local = artifact_identity(manifest, inspected)
                self.assertEqual(local, {'revision':'candidate', 'images':ids})
                published = {**manifest,'images':registry,'tested_image_ids':ids}
                self.assertEqual(artifact_identity(published, inspected),local)
                with self.assertRaisesRegex(ValueError,'different tested image'):
                    artifact_identity(published,changed)

    def test_unknown_or_malformed_contracts_fail_before_image_inspection(self):
        contracts = ('1.13', '1.16', '2.0', '1.15.0', 'v1.15', ' 1.15', '1.15 ', '', None, 1.15, True, [], {})
        manifests = [{'staff_boundary':1, 'contract':contract} for contract in contracts]
        manifests.append({'staff_boundary':1})
        with patch('release.subprocess.check_output') as inspect:
            for manifest in manifests:
                with self.subTest(manifest=manifest):
                    with self.assertRaisesRegex(ValueError, 'minimum employee/contract boundary'):
                        artifact_identity(manifest)
            inspect.assert_not_called()

    def test_supported_contracts_still_require_the_employee_boundary(self):
        with patch('release.subprocess.check_output') as inspect:
            for contract in ('1.14', '1.15'):
                for boundary in (None, 0, 2, '1'):
                    with self.subTest(contract=contract, boundary=boundary):
                        with self.assertRaisesRegex(ValueError, 'minimum employee/contract boundary'):
                            artifact_identity({'contract':contract, 'staff_boundary':boundary})
            inspect.assert_not_called()

    def test_supported_contracts_still_require_exactly_three_images(self):
        with patch('release.subprocess.check_output') as inspect:
            for contract in ('1.14', '1.15'):
                for images in ({'backend':'id'}, {'backend':'a', 'frontend':'b', 'sql':'c', 'extra':'d'}):
                    with self.subTest(contract=contract, images=images):
                        with self.assertRaisesRegex(ValueError, 'Exactly three'):
                            artifact_identity({'contract':contract, 'staff_boundary':1, 'images':images})
            inspect.assert_not_called()

    def test_supported_contracts_still_reject_wrong_image_labels_and_local_ids(self):
        ids = {name:'sha256:' + str(index) * 64 for index, name in enumerate(['backend','frontend','sql'], 1)}
        labels = {'ai-helpdesk.staff-boundary':'1', 'org.opencontainers.image.revision':'candidate'}
        for contract in ('1.14', '1.15'):
            manifest = {'revision':'candidate', 'staff_boundary':1, 'contract':contract, 'images':ids}
            for bad_labels in ({}, {**labels, 'ai-helpdesk.staff-boundary':'0'}, {**labels, 'org.opencontainers.image.revision':'other'}):
                with self.subTest(contract=contract, labels=bad_labels):
                    with self.assertRaisesRegex(ValueError, 'does not match the release manifest'):
                        artifact_identity(manifest, lambda reference: {'Id':reference, 'Config':{'Labels':bad_labels}})
            with self.subTest(contract=contract, local_identity='changed'):
                with self.assertRaisesRegex(ValueError, 'Local immutable identity changed'):
                    artifact_identity(manifest, lambda reference: {'Id':'sha256:' + '9' * 64, 'Config':{'Labels':labels}})

    def test_timestamp_exports_never_open_the_private_backup_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);private=root/'private';private.mkdir(mode=0o700)
            config={'backup_dir':str(private),'report_root':str(root/'reports')}
            publish_metric_status(config,'last-backup.json','completed_at',123)
            file=root/'reports/last-backup.json'
            self.assertEqual(json.loads(file.read_text()),{'completed_at':123})
            self.assertEqual(file.stat().st_mode&0o777,0o644)
            self.assertEqual(private.stat().st_mode&0o777,0o700)
            with self.assertRaisesRegex(ValueError,'private directories'):
                publish_metric_status({**config,'report_root':str(private)},'last-backup.json','completed_at',123)
            self.assertEqual(private.stat().st_mode&0o777,0o700)

    def test_same_revision_with_different_images_is_rejected_before_publication(self):
        manifest={'revision':'same-commit','images':{'backend':'sha256:one','frontend':'sha256:two','sql':'sha256:three'}}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json'
            atomic_json(path,{'completed_at':1,'tested_image_ids':{**manifest['images'],'backend':'sha256:rebuilt'}})
            with self.assertRaisesRegex(ValueError,'different image artifacts'):
                verification(manifest,[path])

    def test_missing_or_extra_service_identities_are_rejected(self):
        for images in [{'backend':'id'}, {'backend':'a','frontend':'b','sql':'c','extra':'d'}]:
            with self.assertRaisesRegex(ValueError,'Exactly three'):
                verification({'images':images},[])


if __name__ == '__main__':
    unittest.main()
