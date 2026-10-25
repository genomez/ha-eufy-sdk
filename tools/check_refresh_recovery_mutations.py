"""Check that recovery regressions fail offline tests, restoring source each time."""

# ruff: noqa: INP001, T201 - command-line result summary

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components/eufy_sdk/coordinator.py"
TEST = "tests/test_refresh_recovery.py::RefreshRecoveryTests::"
MUTATIONS = (
    (
        "healthy-guard",
        "if not self._recovery_active or self._recovery_event_used:",
        "if self._recovery_event_used:",
        "test_fresh_event_burst_requests_one_refresh_and_healthy_requests_none",
    ),
    (
        "rate-limit",
        "if remaining > 0:",
        "if remaining > float('inf'):",
        "test_event_inside_limit_is_deferred_and_recovers_before_retry",
    ),
    (
        "one-shot-retry",
        "if self._fast_retry_cancel is not None:",
        "if self._fast_retry_cancel is not None or self._recovery_step > 0:",
        "test_schedule_repeats_300_until_complete_success",
    ),
)


def main() -> int:
    """Run bounded mutations against the same socket-free test harness."""
    original = SOURCE.read_bytes()
    output = ROOT / ".offline"
    output.mkdir(exist_ok=True)
    failures = 0
    for name, before, after, test in MUTATIONS:
        try:
            text = original.decode()
            if text.count(before) != 1:
                print(f"{name}: mutation target must match exactly once")
                failures += 1
                continue
            SOURCE.write_bytes(text.replace(before, after).encode())
            result = subprocess.run(  # noqa: S603 - fixed local offline runner
                [sys.executable, "-B", "tools/run_offline_tests.py", TEST + test],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            log = result.stdout + result.stderr
            (output / f"mutation-{name}.log").write_text(log)
            detected = result.returncode == 1 and f"FAILED {TEST}{test}" in log
            print(f"{name}: {'detected' if detected else 'NOT detected'}; reverted")
            failures += not detected
        finally:
            SOURCE.write_bytes(original)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
