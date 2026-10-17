"""
Run local HA tests without sockets, subprocesses or writes outside this tree.

Reuse an existing HA/pytest interpreter: python -B tools/run_offline_tests.py
[pytest selections/options]. Plain async tests use an offline loop because the
local test environment does not include pytest-asyncio. Outputs stay in .offline.
"""

# Modified for genomez S1 experimental source preview, 2026-10-17.
# Test adapters mirror private pytest and asyncio interfaces without I/O.
# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, ANN204, ARG002
# ruff: noqa: D102, D107, E402, EM101, INP001, SLF001, TRY003
import asyncio
import inspect
import os
import selectors
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".offline"
OUTPUT.mkdir(exist_ok=True)
TEMP = OUTPUT / "temp"
TEMP.mkdir(exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(TEMP)
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
sys.dont_write_bytecode = True
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


class MemorySelector(selectors.BaseSelector):
    """Drive timers without allocating a network selector or socket."""

    def register(self, *args, **kwargs):
        raise RuntimeError("I/O disabled in offline tests")

    def unregister(self, *args, **kwargs):
        raise RuntimeError("I/O disabled in offline tests")

    def get_map(self):
        return {}

    def select(self, timeout=None):
        if timeout:
            time.sleep(min(timeout, 0.01))
        return []


class OfflineLoop(asyncio.SelectorEventLoop):
    """Allow synthetic coroutine tests while rejecting external I/O."""

    def __init__(self):
        super().__init__(selector=MemorySelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        pass


unittest.IsolatedAsyncioTestCase.loop_factory = OfflineLoop


def audit(event, args):
    """Confine test writes and reject connection attempts and subprocesses."""
    if event in (
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "subprocess.Popen",
    ):
        raise RuntimeError("External I/O disabled in offline tests")
    targets = []
    if event == "open":
        path, _mode, flags = args
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            targets = [path]
    elif event in ("os.mkdir", "os.remove", "os.rmdir"):
        targets = [args[0]]
    elif event in ("os.rename", "os.replace"):
        targets = args[:2]
    for target in targets:
        if str(target).lower() in ("nul", r"\\.\nul"):
            continue
        if not isinstance(target, int) and not Path(target).resolve().is_relative_to(
            ROOT
        ):
            raise RuntimeError("Write outside worktree disabled: " + str(target))


sys.addaudithook(audit)
import pytest


class OfflineAsync:
    """Execute plain pytest coroutine functions on the same offline loop."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(self, pyfuncitem):
        if inspect.iscoroutinefunction(pyfuncitem.obj):
            kwargs = {
                name: pyfuncitem.funcargs[name]
                for name in pyfuncitem._fixtureinfo.argnames
            }
            with asyncio.Runner(loop_factory=OfflineLoop) as runner:
                runner.run(pyfuncitem.obj(**kwargs))
            return True
        return None


if __name__ == "__main__":
    sys.exit(
        pytest.main(
            [
                "-q",
                "-p",
                "no:faulthandler",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(TEMP / "pytest"),
                "-o",
                "addopts=",
                *(sys.argv[1:] or ["tests"]),
            ],
            plugins=[OfflineAsync()],
        )
    )
