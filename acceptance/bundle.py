"""Package and receive an externally measured acceptance bundle as data only.

Publication consumes report.json. Its bytes are copied from the chosen
assembled report, without editing or relabeling any recording or image ID.
No scripts from an incoming archive are executed and no images are rebuilt.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

from acceptance.common import canonical, read, sha
from acceptance.gate import require


def files_for_report(report):
    root = report.resolve().parent
    found = {}
    def visit(value):
        if isinstance(value, dict):
            if "evidence_file" in value:
                name = value["evidence_file"]
                source = (root / name).resolve()
                if not source.is_relative_to(root) or not source.is_file() or source.is_symlink():
                    raise ValueError("Missing or escaped bundle evidence")
                payload = source.read_bytes()
                if sha(payload) != value.get("evidence_sha256"):
                    raise ValueError("Evidence digest changed while packaging")
                if name not in found:
                    found[name] = payload
                    visit(json.loads(payload))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(read(report))
    found["original-report/" + report.name] = report.read_bytes()
    found["report.json"] = report.read_bytes()
    return found


def pack(report, manifest, output):
    report, output = Path(report), Path(output)
    if output.exists():
        raise ValueError("Never overwrite a previous acceptance bundle")
    require(report, read(manifest))
    files = files_for_report(report)
    files["release.json"] = Path(manifest).read_bytes()
    index = {"format": 1, "revision": read(manifest)["revision"], "files": {k: sha(v) for k, v in files.items()}}
    files["bundle-index.json"] = canonical(index)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(files.items()):
            archive.writestr(name, payload)
    return {"bundle": str(output.resolve()), "sha256": sha(output.read_bytes()), "files": len(files)}


def receive(bundle, expected_sha, output, revision):
    bundle, output = Path(bundle), Path(output)
    if sha(bundle.read_bytes()) != expected_sha:
        raise ValueError("Incoming bundle differs from the separately supplied SHA-256")
    if output.exists():
        raise ValueError("Receipt directory must be new")
    with zipfile.ZipFile(bundle) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 3_000_000_000:
            raise ValueError("Acceptance bundle exceeds bounded data limits")
        names = [e.filename for e in entries]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate archive paths")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Unsafe archive path or symlink")
        index = json.loads(archive.read("bundle-index.json"))
        if index.get("format") != 1 or index.get("revision") != revision or set(index.get("files", {})) != set(names) - {"bundle-index.json"}:
            raise ValueError("Bundle index or frozen source revision differs")
        for name, digest in index["files"].items():
            if sha(archive.read(name)) != digest:
                raise ValueError("Archived evidence digest differs")
        output.mkdir(parents=True)
        for entry in entries:
            target = output / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
    result = require(output / "report.json", read(output / "release.json"))
    if result["artifacts"]["revision"] != revision:
        raise ValueError("Acceptance images differ from the checked-out frozen commit")
    return {"passed": True, "revision": revision, "report": str((output / "report.json").resolve())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("pack")
    export.add_argument("--report", type=Path, required=True)
    export.add_argument("--manifest", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    consume = commands.add_parser("receive")
    consume.add_argument("--bundle", type=Path, required=True)
    consume.add_argument("--sha256", required=True)
    consume.add_argument("--output", type=Path, required=True)
    consume.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(pack(args.report, args.manifest, args.output) if args.command == "pack" else receive(args.bundle, args.sha256, args.output, args.revision))
