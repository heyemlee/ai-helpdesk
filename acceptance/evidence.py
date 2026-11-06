"""Content-addressed evidence references, constrained to one report directory."""
from __future__ import annotations

from pathlib import Path

from acceptance.common import read, sha


def load_evidence(root, reference, *, artifacts=None, config_sha256=None):
    root = Path(root).resolve()
    relative = reference.get("evidence_file")
    expected = reference.get("evidence_sha256")
    if not isinstance(relative, str) or not relative or not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("Evidence file and SHA-256 are required")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root or not path.is_file():
        raise ValueError("Evidence file missing or outside report directory")
    if sha(path.read_bytes()) != expected:
        raise ValueError("Evidence content digest mismatch: " + relative)
    data = read(path)
    if artifacts is not None and data.get("artifacts") != artifacts:
        raise ValueError("Evidence belongs to different application artifacts: " + relative)
    if config_sha256 is not None and data.get("config_sha256") != config_sha256:
        raise ValueError("Evidence belongs to different configuration: " + relative)
    return data
