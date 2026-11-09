"""Select an immutable accepted candidate from the repository's trusted history."""
import argparse
import re
import subprocess

AUTH_BOUNDARY = "34b4298f3e1966d71c7ed92d4c1bdabb06727291"


def validate_revision(revision, trusted_main="refs/remotes/origin/main"):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Select the full immutable candidate commit")
    if subprocess.check_output(["git", "cat-file", "-t", revision], text=True).strip() != "commit":
        raise ValueError("Candidate is not a commit")
    for older, newer in ((AUTH_BOUNDARY, revision), (revision, trusted_main)):
        if subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], capture_output=True).returncode:
            raise ValueError("Candidate is outside trusted main history or predates the accepted authentication boundary")
    return revision


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("--trusted-main", default="refs/remotes/origin/main")
    args = parser.parse_args()
    print(validate_revision(args.revision, args.trusted_main))
