"""Stable files and provenance; no application configuration is imported."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "acceptance" / "data" / "v3"
GROUPS = {"orders": 60, "policies": 50, "cross_message": 50, "images": 40}
BASELINE_REVISION = "5310da9e6ed6dcc2d5a8ee3ec92ea2c0a729ba3d"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def dataset(directory=DATA):
    directory = Path(directory)
    manifest = read(directory / "manifest.json")
    for name, expected in manifest["files"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or sha(path.read_bytes()) != expected:
            raise ValueError("Frozen dataset hash mismatch: " + name)
    return manifest, read(directory / "development.json"), read(directory / "release.json")
