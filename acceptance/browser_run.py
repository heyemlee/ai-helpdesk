"""Run the independent browser recording against an owned temporary stack."""
import argparse
import os
from pathlib import Path
import subprocess

from acceptance.common import ROOT, read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = read(args.stack / "private-config.json")
    if not args.stack.name.startswith("aihd-stage6-") or not config["origin"].startswith("http://127.0.0.1:"):
        raise ValueError("Only an owned temporary stack is allowed")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "en_US.UTF-8",
           "ACCEPTANCE_STACK": str(args.stack), "ACCEPTANCE_BROWSER_OUTPUT": str(args.output),
           "ACCEPTANCE_PLAYWRIGHT_MODULE": str(args.stack / "source/frontend/node_modules/@playwright/test")}
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.is_file():
        env["BROWSER_EXECUTABLE"] = str(chrome)
    subprocess.run([str(args.stack / "source/frontend/node_modules/.bin/playwright"), "test", "-c", str(ROOT / "acceptance/browser/playwright.config.cjs")], env=env, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
