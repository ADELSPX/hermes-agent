"""PROOF-ONLY (wine2e-install/upd-r8-lock-*): does a child that INHERITS the owner's checkout-lock
handle keep the msvcrt byte lock after the owner is killed? (D2's preferred fence.)"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli.update_lock import checkout_lock_held

pytestmark = pytest.mark.platforms("windows")
REPO_ROOT = Path(__file__).resolve().parents[2]

_OWNER = r"""
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli import update_lock as ul
assert ul.UpdateLock(path=Path(sys.argv[3]), install_root=sys.argv[2]).acquire()
fd = ul._HELD["fd"]
os.set_inheritable(fd, True)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], close_fds=False,
                         creationflags=subprocess.CREATE_NO_WINDOW)
print(child.pid, flush=True)
time.sleep(120)
"""


def test_probe_inherited_lock_handle_after_owner_death(tmp_path):
    import psutil

    install = tmp_path / "checkout"
    install.mkdir()
    owner = subprocess.Popen([sys.executable, "-c", _OWNER, str(REPO_ROOT), str(install), str(tmp_path / "m")],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, encoding="utf-8")
    child = psutil.Process(int(owner.stdout.readline().strip()))
    try:
        held_before = checkout_lock_held(install)
        handles = child.num_handles()
        subprocess.run(["taskkill", "/F", "/PID", str(owner.pid)], capture_output=True, check=False)
        owner.wait(timeout=30)
        time.sleep(1)
        alive, held_after = child.is_running(), checkout_lock_held(install)
        print(f"PROBE held_before={held_before} child_alive={alive} child_handles={handles} "
              f"held_after_owner_killed={held_after}")
        assert held_before and alive
    finally:
        child.kill()
