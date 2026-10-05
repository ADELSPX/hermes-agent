"""Regression: Windows gateway pause/resume must feed the #91277 Phase 2
plan-vs-execution reconciliation, not report a correctly-relaunched Windows
gateway as "unaccounted".

``_pause_windows_gateways_for_update`` / ``_resume_windows_gateways_after_update``
are Windows's own gateway restart mechanism — separate from the
systemd/launchd restart phase in ``_cmd_update_impl`` that populates
``restarted_services`` / ``relaunched_profiles`` / ``killed_pids`` /
``externally_supervised_profiles``. Before this fix, a Windows gateway that
was correctly paused and relaunched left no trace in that bookkeeping, so
``match_runtime_outcomes`` classified it "unaccounted" — the plan saw it and
NO bookkeeping mentions it — and ``report_unaccounted_runtimes`` escalated
that into ``sys.exit(1)`` even though the update (and the restart) succeeded.

``_resume_windows_gateways_after_update`` now writes the profiles it
successfully relaunched onto ``token["relaunched_profiles"]``; the update
command merges that into the shared ``relaunched_profiles`` list before
reconciliation runs (mirrored here directly, since driving the full
``_cmd_update_impl`` end to end is impractical).
"""

from unittest.mock import patch

import pytest

import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.main as hm


@pytest.fixture(autouse=True)
def _stub_post_relaunch_liveness(monkeypatch):
    """The resume path now verifies a stable gateway process actually exists
    before vouching for the relaunch (#48820 3rd/4th repro — a parent Job
    Object killing the respawned gateway made '✓ Restarting' a lie). These
    reconciliation tests exercise the token bookkeeping, not the liveness
    poll, so stub it as 'gateway came up'."""
    monkeypatch.setattr(
        gateway_windows, "_wait_for_gateway_ready", lambda **_kw: [4242]
    )
    monkeypatch.setattr(
        gateway_windows, "_write_start_attestation", lambda *_a, **_kw: None
    )


def test_merge_helper_reads_token_keys_into_restart_outcome(monkeypatch):
    """Drive the real merge helper (not a mirror): the Windows resume token's
    ``relaunched_profiles`` / ``restarted_services`` / ``service_profiles`` /
    ``services`` keys must land in the shared restart bookkeeping."""
    from hermes_cli import update_cmd

    monkeypatch.setattr(hm, "_resume_windows_gateways_after_update", lambda token: None)
    outcome = update_cmd._GatewayRestartOutcome(
        incomplete=False,
        phase_errors=[],
        pre_restart_gateway_pids=[],
        restarted_services=["hermes-gateway"],
        failed_or_stale_units=[],
        relaunched_profiles=[],
        externally_supervised_profiles=[],
        killed_pids=set(),
    )
    token = {
        "resume_needed": False,
        "relaunched_profiles": ["p1"],
        "restarted_services": ["svc"],
        "service_profiles": {"svc": "p2", "pending": "p3"},
        "services": ["pending"],
    }
    with patch("hermes_cli.update_receipt.record_gateway_restart", lambda **kw: None):
        update_cmd._resume_windows_gateways_and_merge_outcome(outcome, token, False)

    assert outcome.relaunched_profiles == ["p1", "p2"]
    assert outcome.restarted_services == ["hermes-gateway", "svc"]
    assert outcome.failed_or_stale_units == ["p3"]
    assert outcome.incomplete is False


# ---------------------------------------------------------------------------
# #115563: symmetric resume-failure handling + atexit double-fire
# ---------------------------------------------------------------------------

def test_resume_unregisters_its_own_atexit_fallback_before_running(monkeypatch):
    """Every foreground call site registers this same function via atexit as a dead-process
    safety net. Once execution actually reaches here it must disarm that fallback immediately
    -- otherwise a failure below (or the foreground caller dying right after return) replays
    the identical error a second time at interpreter teardown (#115563)."""
    import atexit

    from hermes_cli import update_cmd_windows

    calls = []
    monkeypatch.setattr(
        atexit, "unregister",
        lambda fn: calls.append(fn) or None,
    )
    monkeypatch.setattr(hm, "_is_windows", lambda: False)

    token = {"resume_needed": True}
    update_cmd_windows._resume_windows_gateways_after_update(token)

    from hermes_cli import update_cmd

    assert calls == [update_cmd_windows._resume_windows_gateways_after_update,
                     update_cmd._resume_paused_gateways_at_exit]
    assert token["resume_needed"] is False


def test_the_update_commands_atexit_net_reports_an_owed_resume_instead_of_raising(monkeypatch, tmp_path):
    """R9-4: the net ``hermes update`` arms for gateways it paused (after the commit point the git
    route's only one) records a refused resume as the ``windows_resume`` follow-up on the run's
    finalized receipt; it never raises inside atexit."""
    import atexit
    from types import SimpleNamespace

    from hermes_cli import update_cmd, update_receipt

    class Stop(Exception):
        pass

    nets = []

    def register(fn, *args, **kwargs):
        nets.append((fn, args, kwargs))

    def unregister(fn):
        nets[:] = [net for net in nets if net[0] != fn]

    def refused(resume_token):
        raise RuntimeError("Could not restart Windows gateway service(s): HermesGatewayProbe")

    def stop_after_the_request_is_built(args):
        raise Stop

    token = {"resume_needed": True, "profiles": {}, "unmapped": [], "services": ["HermesGatewayProbe"]}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(atexit, "register", register)
    monkeypatch.setattr(atexit, "unregister", unregister)
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda args, gateway_mode: SimpleNamespace(
        gw_input_fn=None, assume_yes=True, pre_update_version=None))
    monkeypatch.setattr(update_cmd, "_begin_update_receipt_and_plan",
                        lambda args: update_receipt.begin_update_receipt())
    monkeypatch.setattr(hm, "_run_pre_update_backup", lambda args: None)
    monkeypatch.setattr(update_cmd, "_record_pre_update_backup_outcome", lambda *a: None)
    monkeypatch.setattr(update_cmd, "_record_snapshot_stage", lambda *a: None)
    monkeypatch.setattr(hm, "_pause_windows_gateways_for_update", lambda: token)
    monkeypatch.setattr(hm, "_resume_windows_gateways_after_update", refused)
    monkeypatch.setattr(hm, "_desktop_packaged_executable", lambda d: None)
    monkeypatch.setattr(hm, "_desktop_dist_exists", lambda d: False)
    monkeypatch.setattr(hm, "_installed_desktop_apps", lambda: [])
    monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda: (False, ["git"], False))
    monkeypatch.setattr(hm, "_resolve_update_branch", stop_after_the_request_is_built)
    try:
        with pytest.raises(Stop):
            update_cmd._cmd_update_impl(SimpleNamespace(branch="main", yes=True), False)
        update_receipt.finalize_pending_update_receipt(1)  # the command boundary, before atexit
        assert update_receipt._current.get() is None
        update_id = update_receipt.read_latest_receipt()["update_id"]
        armed = [net for net in nets if net[1][:1] == (token,)]
        assert len(armed) == 1, nets
        fn, args, kwargs = armed[0]
        escaped = None
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # what atexit would print as a traceback
            escaped = exc
        assert escaped is None, f"the atexit net raised: {escaped!r}"
        latest = update_receipt.read_latest_receipt()
        assert latest["update_id"] == update_id
        assert [row["step"] for row in latest.get("followups") or []] == ["windows_resume"]
    finally:
        update_receipt._current.set(None)
