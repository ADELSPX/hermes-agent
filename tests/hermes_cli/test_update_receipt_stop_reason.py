"""A bare ``sys.exit(1)`` must not be the whole post-mortem of a failed update (#132089).

``_cmd_update_impl`` has ~25 early ``sys.exit`` sites, each of which prints its own ``✗ …``
line first. Under a scheduler that discards stdout (launchd, cron, a fleet wrapper) that
print is gone by the time anyone reads the receipt, so the boundary stamped the exit code
alone: every recorded step green, ``stop_reason: "sys.exit(1)"``, no step named.

Two mechanisms, both pinned here against the real receipt API:

1. ``record_current_step`` names the step in progress, so a death between two recorded
   steps still says where.
2. A failing ``record_step`` detail is folded into the stop reason, because that detail IS
   the reason the step gave.

The success path must keep its existing reason verbatim — a receipt that claims success with
a diagnostic suffix on it is a different bug, not a stricter version of this one.
"""

import inspect
import json
from types import SimpleNamespace

import pytest

import hermes_cli.update_receipt as ur
from hermes_cli import update_cmd


@pytest.fixture()
def receipt_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with ur.update_receipt_scope():
        yield home


def _payload(path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestBareExitNamesItsStep:

    def test_exit_in_the_middle_of_a_step_names_it(self, receipt_home):
        """The reported shape: plan and snapshot green, then a dead process with no reason."""
        ur.begin_update_receipt()
        ur.record_stage("plan", "success")
        ur.record_stage("snapshot", "success")
        ur.record_current_step("fetch")

        path = ur.finalize_pending_update_receipt(1, "sys.exit(1)")
        payload = _payload(path)

        assert payload["outcome"] == "failed"
        # The old receipt said only this; the step is the addition.
        assert payload["stop_reason"] == "sys.exit(1) during fetch"
        assert payload["exit_code"] == 1
        # The two completed stages are untouched — this adds context, it does not rewrite history.
        assert [stage["name"] for stage in payload["stages"]] == ["plan", "snapshot"]
        assert [stage["outcome"] for stage in payload["stages"]] == ["success", "success"]

    def test_failing_step_detail_is_folded_into_the_reason(self, receipt_home):
        """A step that recorded its own failure already knows the why; surface it."""
        ur.begin_update_receipt()
        ur.record_current_step("fetch")
        ur.record_step("git_fetch", False, "fatal: unable to access remote: Connection timed out")

        payload = _payload(ur.finalize_pending_update_receipt(1, "sys.exit(1)"))

        assert payload["stop_reason"] == (
            "sys.exit(1) during fetch "
            "(git_fetch: fatal: unable to access remote: Connection timed out)"
        )

    def test_detail_alone_is_enough_when_no_step_was_marked(self, receipt_home):
        """Older call sites that record only a failing step still get a diagnosable reason."""
        ur.begin_update_receipt()
        ur.record_step("windows_preflight", False, "hermes.exe holds venv")

        payload = _payload(ur.finalize_pending_update_receipt(2, "sys.exit(2)"))

        assert payload["stop_reason"] == "sys.exit(2) during windows_preflight: hermes.exe holds venv"

    def test_a_successful_run_keeps_its_plain_reason(self, receipt_home):
        """Success must not grow a diagnostic suffix; the reader keys on that reason."""
        ur.begin_update_receipt()
        ur.record_current_step("completion")

        payload = _payload(ur.finalize_pending_update_receipt(0, ur.COMMAND_BOUNDARY_STOP_REASON))

        assert payload["outcome"] == "success"
        assert payload["stop_reason"] == ur.COMMAND_BOUNDARY_STOP_REASON

    def test_nothing_recorded_leaves_the_reason_untouched(self, receipt_home):
        """No step marked and no failure: the suffix is empty, not a dangling ' during'."""
        ur.begin_update_receipt()

        payload = _payload(ur.finalize_pending_update_receipt(1, "sys.exit(1)"))

        assert payload["stop_reason"] == "sys.exit(1)"

    def test_context_is_empty_without_a_receipt(self):
        """Never raises and never invents a step for a run that recorded nothing."""
        assert ur.exit_context() == ""

    def test_a_green_step_does_not_become_the_failure(self, receipt_home):
        """Only ok=False carries a why; a passing step must not be reported as the failure."""
        ur.begin_update_receipt()
        ur.record_current_step("restart")
        ur.record_step("gateway_restart", True, "3 unit(s) restarted")

        payload = _payload(ur.finalize_pending_update_receipt(1, "sys.exit(1)"))

        assert payload["stop_reason"] == "sys.exit(1) during restart"
        assert "gateway_restart" not in payload["stop_reason"]


class TestDebrisSweepCoversTheSnapshotWindow:

    """The lock sweep used to run only in the apply path, after the snapshot.

    A run that died in between left ``.git/index.lock`` behind with nothing in this run to
    remove it, so the NEXT run failed with "File exists" until an operator did it by hand.
    """

    def test_impl_sweeps_before_the_plan_and_snapshot(self, receipt_home, monkeypatch):
        """The sweep must run before the first mutation, not only before the fetch."""
        order = []

        class _FakeMain:
            """The window's own members; anything past it fails the test instead of running."""

            PROJECT_ROOT = receipt_home / "repo"

            @staticmethod
            def _run_pre_update_backup(args):
                order.append("snapshot")
                return "20261003-020409-pre-update"

            @staticmethod
            def _pause_windows_gateways_for_update():
                return None

            def __getattr__(self, name):  # the desktop/fleet probes past this window
                raise AssertionError(f"flow continued past the window via {name}")

        monkeypatch.setattr(update_cmd, "git_operation_in_progress", lambda root: None)
        monkeypatch.setattr(
            update_cmd._check, "clear_git_debris", lambda root: order.append("sweep") or None
        )
        monkeypatch.setattr(
            update_cmd, "_resolve_update_options",
            lambda args, gateway_mode: SimpleNamespace(gw_input_fn=None, assume_yes=True),
        )
        monkeypatch.setattr(
            update_cmd, "_begin_update_receipt_and_plan", lambda args: order.append("plan") or None
        )
        monkeypatch.setattr(update_cmd, "_m", _FakeMain)
        monkeypatch.setattr(update_cmd, "_record_pre_update_backup_outcome", lambda *a: None)
        monkeypatch.setattr(update_cmd, "_record_snapshot_stage", lambda *a: None)

        with pytest.raises(AssertionError):
            update_cmd._cmd_update_impl(SimpleNamespace(), gateway_mode=False)

        assert order[:3] == ["sweep", "plan", "snapshot"]

    def test_the_apply_path_no_longer_sweeps_a_second_time(self):
        """One sweep at the start is the contract; a duplicate would print the same line twice."""
        source = inspect.getsource(update_cmd._cmd_update_impl)
        assert "clear_stale_git_locks" not in source
        assert "clear_stale_tmp_packs" not in source
        # The shared helper is what both the start sweep and --check go through.
        assert source.count("clear_git_debris(_m().PROJECT_ROOT)") == 1
