"""Real-provider mount layout, using synthetic credentials and a separate ledger."""
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acceptance.common import DATA, ROOT, read, sha, write
from acceptance.container_stack import ContainerStack


@contextlib.contextmanager
def synthetic_stack(manifest, *, inspect_images=False):
    with tempfile.TemporaryDirectory(prefix="aihd-mount-fixture-", dir="/private/tmp") as folder:
        source = Path(folder)
        shutil.copytree(ROOT / "acceptance", source / "acceptance", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        settings = source / "backend/data/runtime/model_settings.json"
        write(settings, {"chat": {"values": {"provider": "openai", "base_url": "https://supplier.invalid/v1",
                                             "api_key": "synthetic-unused-test-key"}}}, private=True)
        approval = {"scope": "release_and_baseline", "confirmed": True, "id": "synthetic-mount-check",
                    "user_approval_reference": "offline-test-only", "requests": 1, "scenarios": 1,
                    "model": "gpt-6.1-sol", "max_output_tokens": 2048,
                    "supplier_base_url": "https://supplier.invalid/v1", "corpus_sha256": sha((DATA / "manifest.json").read_bytes())}
        write(source / "approval.json", approval)
        with contextlib.ExitStack() as mocks:
            mocks.enter_context(patch("acceptance.container_stack.ROOT", source))
            mocks.enter_context(patch("acceptance.gateway.ROOT", source))
            mocks.enter_context(patch("acceptance.container_stack.business"))
            if not inspect_images:
                mocks.enter_context(patch("acceptance.container_stack.port", side_effect=range(31000, 31006)))
                mocks.enter_context(patch("acceptance.container_stack.subprocess.check_output", side_effect=lambda args: json.dumps([
                    {"Id": args[3], "Config": {"Labels": {"org.opencontainers.image.revision": manifest["revision"]}}}
                ]).encode()))
            stack = ContainerStack(manifest, split="development", provider="real", budget=source / "approval.json")
        try:
            yield stack, source
        finally:
            shutil.rmtree(stack.root)


class ContainerMountTests(unittest.TestCase):
    def test_real_stack_prepares_empty_nested_mountpoints_without_copying_credentials(self):
        manifest = {"revision": "test-revision", "images": {name: "sha256:" + char * 64 for name, char in
                                                               (("backend", "a"), ("frontend", "b"), ("sql", "c"))}}
        with synthetic_stack(manifest) as (stack, source):
            helpers = stack.root / "harness"
            self.assertTrue((helpers / ".stage6").is_dir())
            self.assertEqual(list((helpers / ".stage6").iterdir()), [])
            self.assertEqual((helpers / "backend/data/runtime/model_settings.json").read_bytes(), b"")
            mounts = stack.spec["services"]["provider"]["volumes"]
            self.assertIn(str(helpers) + ":/opt/harness:ro", mounts)
            self.assertIn(str(source / ".stage6") + ":/opt/harness/.stage6", mounts)
            self.assertIn(str(source / "backend/data/runtime/model_settings.json") + ":/opt/harness/backend/data/runtime/model_settings.json:ro", mounts)
            self.assertEqual(list((helpers / "backend").rglob("*.json")), [helpers / "backend/data/runtime/model_settings.json"])


@unittest.skipUnless(os.getenv("ACCEPTANCE_MOUNT_MANIFEST"), "Set ACCEPTANCE_MOUNT_MANIFEST for the network-disabled Docker regression")
class DockerContainerMountTests(unittest.TestCase):
    def test_real_gateway_starts_with_readonly_parent_and_zero_model_requests(self):
        manifest = read(os.environ["ACCEPTANCE_MOUNT_MANIFEST"])
        output = Path(os.environ["ACCEPTANCE_MOUNT_OUTPUT"])
        output.mkdir(parents=True, exist_ok=False)
        with synthetic_stack(manifest, inspect_images=True) as (stack, source):
            provider = stack.spec["services"]["provider"]
            provider["network_mode"] = "none"
            provider.pop("ports")
            stack.spec = {"services": {"provider": provider}}
            write(stack.root / "compose.json", stack.spec, private=True)
            try:
                stack.run("up", "-d", "--wait", "provider")
                checks = json.loads(stack.run("exec", "-T", "provider", "python", "-c", """
import errno, json, sqlite3, urllib.request
from pathlib import Path
health=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2))
assert health['healthy'] and health['provider_enabled'] and health['remaining_requests']==1
for path in (Path('/opt/harness/new-file'),Path('/opt/harness/backend/data/runtime/model_settings.json')):
    try:
        with path.open('ab'): pass
    except OSError as exc:
        assert exc.errno==errno.EROFS, str(exc)
    else:
        raise AssertionError('Read-only mount allowed a write')
ledger=Path('/opt/harness/.stage6/synthetic-mount-check.sqlite')
with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as db:
    counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('attempts','receipts')}
assert counts=={'attempts':0,'receipts':0}, counts
probe=ledger.parent/'writable-probe'
probe.write_text('synthetic')
probe.unlink()
print(json.dumps({'health':health,'parent_readonly':True,'settings_readonly':True,'ledger_writable':True,**counts}))
"""))
                container = stack.run("ps", "-q", "provider").strip()
                network = subprocess.check_output(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", container]).decode().strip()
                self.assertEqual(network, "none")
                self.assertEqual((stack.root / "harness/backend/data/runtime/model_settings.json").read_bytes(), b"")
                write(output / "result.json", {"passed": True, "product_revision": manifest["revision"],
                      "backend_image": manifest["images"]["backend"], "harness_sha256": stack.harness_sha,
                      "network_mode": network, "real_credentials_mounted": False, "checks": checks})
            finally:
                try:
                    stack.close()
                finally:
                    shutil.copyfile(stack.root / "compose.log", output / "compose.log")
