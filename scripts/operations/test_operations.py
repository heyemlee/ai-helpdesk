"""Failure boundaries for encrypted snapshots and conservative local retention."""
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from recovery import Operations, atomic_json, crypt, digest, publish_metric_status
from publish_release import verification
from verify import _verify
from release import artifact_identity


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


class PublicationTests(unittest.TestCase):
    def test_published_digest_resolves_to_exact_tested_images(self):
        ids = {name: 'sha256:' + str(index) * 64 for index, name in enumerate(['backend','frontend','sql'], 1)}
        registry = {name: 'example.invalid/app/' + name + '@sha256:' + str(index) * 64 for index, name in enumerate(ids, 4)}
        manifest = {'revision':'candidate','staff_boundary':1,'contract':'1.14','images':ids}
        def inspected(reference):
            service = next(name for name in ids if reference in {ids[name],registry[name]})
            return {'Id':ids[service],'RepoDigests':[registry[service]],'Config':{'Labels':{'ai-helpdesk.staff-boundary':'1','org.opencontainers.image.revision':'candidate'}}}
        local = artifact_identity(manifest, inspected)
        published = {**manifest,'images':registry,'tested_image_ids':ids}
        self.assertEqual(artifact_identity(published, inspected),local)
        def changed(reference):
            info = inspected(reference)
            if reference == registry['backend']:info['Id']='sha256:'+'9'*64
            return info
        with self.assertRaisesRegex(ValueError,'different tested image'):
            artifact_identity(published,changed)
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
