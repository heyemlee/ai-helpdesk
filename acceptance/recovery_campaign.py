"""Execute a fresh 200-Case recovery drill and emit its bound components."""
import argparse
from pathlib import Path
import subprocess
import sys

from acceptance.common import ROOT, read, write
from acceptance.recovery_evidence import component


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operations-python", default=sys.executable)
    parser.add_argument("--s3", action="store_true")
    args = parser.parse_args()
    if args.output.exists(): raise ValueError("Never overwrite recovery evidence")
    args.output.mkdir(parents=True)
    kind = "recovery_s3" if args.s3 else "recovery_local"
    raw = args.output / "raw.json"
    subprocess.run([args.operations_python, str(ROOT / "scripts/operations/drill.py"), "--manifest", str(args.manifest),
                    "--report", str(raw), "--case-count", "200", "--acceptance", *(["--s3"] if args.s3 else [])], check=True)
    write(args.output / (kind + ".json"), component(kind, read(raw)))
    if not args.s3: write(args.output / "rollback.json", component("rollback", read(raw)))
