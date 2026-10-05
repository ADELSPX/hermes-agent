"""An update that dies between the snapshot and the apply stage is attributed and leaves no lock (#132089).

Such runs used to finalize as ``stages: [plan, snapshot]`` with ``stop_reason: "sys.exit(1)"``,
which shared metrics count as failed_stage=apply / apply_mode=unknown, and the version probe
that opens the receipt could strand ``.git/index.lock`` for the same run's merge to die on.
"""

import json
import subprocess
import sys

import pytest

import hermes_cli.update_receipt as ur
from hermes_cli.observability.shared_metrics_update import update_receipt_fields
from hermes_cli.version_info import _git_version_info


def test_exit_inside_the_apply_window_is_an_apply_failure_with_its_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with ur.update_receipt_scope():
        ur.begin_update_receipt()
        ur.record_stage("plan", "success")
        ur.record_stage("snapshot", "success")
        ur.record_current_step("fetch")
        printed = "✗ Failed to fetch updates from origin. fatal: unable to access 'https://github.com/'"
        receipt = json.loads(ur.finalize_pending_update_receipt(1, "sys.exit(1)", printed).read_text(encoding="utf-8"))

    assert receipt["stop_reason"] == f"sys.exit(1) during fetch: {printed}"
    run, stages = update_receipt_fields(receipt)
    assert (run["outcome"], run["failed_stage"], run["apply_mode"]) == ("failed", "apply", "git")
    assert [(row["stage"], row["outcome"]) for row in stages][-1] == ("apply", "failed")


@pytest.mark.skipif(sys.platform == "win32", reason="the slow remote is a POSIX shell wrapper")
def test_a_timed_out_version_probe_never_strands_index_lock(tmp_path):
    """A partial clone's status lazily fetches the HEAD tree while holding index.lock; the probe's
    timeout used to kill it there and leave the lock for the update's merge to fail on."""
    def git(*args, cwd=tmp_path):
        subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                       cwd=cwd, check=True, capture_output=True)

    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    git("init", "-q", "--bare", str(origin))
    git("config", "uploadpack.allowFilter", "true", cwd=origin)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=origin)
    git("init", "-q", str(work))
    for name in ("a", "b"):
        (work / name).mkdir()
        (work / name / "f.txt").write_text(name, encoding="utf-8")
        git("add", "-A", cwd=work)
        git("commit", "-qm", name, cwd=work)
        git("push", "-q", str(origin), "HEAD:refs/heads/main", cwd=work)
        if name == "a":
            git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
            git("clone", "-q", "--filter=tree:0", origin.as_uri(), str(clone))
    git("fetch", "-q", "origin", cwd=clone)
    git("reset", "-q", "--soft", "origin/main", cwd=clone)  # HEAD's tree now lives only on the remote
    slow = tmp_path / "slow-upload-pack"
    slow.write_text('#!/bin/sh\nsleep 5\nexec git-upload-pack "$@"\n', encoding="utf-8")
    slow.chmod(0o755)
    git("config", "remote.origin.uploadpack", str(slow), cwd=clone)

    _git_version_info(clone)

    assert not (clone / ".git" / "index.lock").exists()
