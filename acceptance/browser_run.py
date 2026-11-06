"""Run the independent browser recording against an owned temporary stack."""
import argparse
import os
from pathlib import Path
import subprocess

from acceptance.common import DATA, ROOT, read, write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-modules", type=Path)
    parser.add_argument("--real-sample", action="store_true")
    parser.add_argument("--budget", type=Path)
    args = parser.parse_args()
    config = read(args.stack / "private-config.json")
    if not args.stack.name.startswith("aihd-stage6-") or not config["origin"].startswith("http://127.0.0.1:"):
        raise ValueError("Only an owned temporary stack is allowed")
    provenance = read(args.stack / "provenance.json")
    ledger = None
    if provenance["real_provider"]:
        from acceptance.gateway import release_ledger
        if not args.real_sample or not args.budget:
            raise ValueError("Real browser work requires the separately approved release budget")
        ledger = release_ledger(read(args.budget))
        ledger.register_scenario("browser:development-images-001")
    modules = args.node_modules or args.stack / "source/frontend/node_modules"
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "en_US.UTF-8",
           "ACCEPTANCE_STACK": str(args.stack), "ACCEPTANCE_BROWSER_OUTPUT": str(args.output),
           "ACCEPTANCE_PLAYWRIGHT_MODULE": str(modules / "@playwright/test"), "ACCEPTANCE_CORPUS_DIR": str(DATA),
           "ACCEPTANCE_REAL_BROWSER": "1" if ledger else "0"}
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        env["BROWSER_EXECUTABLE"] = str(chrome)
    subprocess.run([str(modules / ".bin/playwright"), "test", "-c", str(ROOT / "acceptance/browser/playwright.config.cjs")], env=env, cwd=ROOT, check=True)
    recording = read(args.output / "recording.json")
    if ledger:
        for run in recording["withImage"]["runs"]:
            ledger.bind_run("helpdesk", run["run_id"], "browser:development-images-001")
    if provenance.get("execution") == "immutable_containers":
        recording.update(execution="immutable_containers", artifacts=provenance["artifacts"],
                         config_sha256=provenance["config_sha256"], source_revision=provenance["artifacts"]["revision"])
    write(args.output / "recording.json", recording)


if __name__ == "__main__":
    main()
