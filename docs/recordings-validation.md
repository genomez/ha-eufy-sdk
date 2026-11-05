# Recordings integration validation — 2026-11-05

Local implementation package P2, branch `recordings-browse-20261105`, based on
`d57d2e9`. Synthetic fixture version: `tests/fixtures/recordings_v1.py`, contract v1
with the 2026-11-05 lead amendments. No captured responses or real identifiers.

The owner corrected the specification's Python 3.13 requirement: the established
environment is Python **3.14.4**, Home Assistant **2026.7.0**. All commands below
ran from the integration repository root, with this interpreter:

```text
C:/Users/jason/OneDrive/Documents/Cursor/eufy/contributions/owned-pr-reconciliation-20261005/ha/checkout/.venv/Scripts/python.exe
```

Command suffixes (prepend the quoted interpreter path):

```text
-B tools/run_offline_tests.py tests/test_recordings_api.py tests/test_recordings_media_source.py tests/test_recordings_http.py tests/test_recordings_privacy.py
-B tools/run_offline_tests.py
```

| Run | Passed | Failed | Skipped | Passed subtests | Warnings |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline, before feature wiring/tests | 922 | 0 | 0 | 1168 | 1 |
| Final recordings tests | 34 | 0 | 0 | 32 | 5 |
| Final full offline suite | 956 | 0 | 0 | 1200 | 5 |

The first full integration run exposed unsupported legacy setup assumptions,
the first-save default-options comparison, and an encoding error in new English
strings. Those were fixed without changing existing tests. Affected regressions
were rerun, followed by the successful final full suite. The warnings are HA HTTP
and backoff deprecations in the installed test dependencies. JSON line endings
were subsequently normalized with an assertion that parsed JSON stayed identical.

Ruff from the same environment's `Scripts/ruff.exe` passed `check` and `format
--check` for all 16 changed/new Python files. `git diff --check` passed.
Local detailed output remains in ignored `.offline/recordings-baseline.log`,
`recordings-focused.log`, `recordings-regression.log`, `recordings-full.log`,
and `recordings-full-first-attempt.log`.

The bridge is mocked. No paired P1 wire fixtures were supplied in this package;
pairing the exact SDK and bridge revisions and independently reviewing P2 remain
lead gates. Per amendment 6, HTTP coverage asserts `requires_auth=True` and exercises
proxy logic without a real HA HTTP/auth server. Anonymous refusal, frontend
authenticated thumbnails, actual desktop/Android navigation and device behavior
remain live acceptance gates. No network, cloud, device, SSH, deployment, push or
tag operation was performed.

The implementation splits recording transport wrappers into `recordings_api.py`
(mixed into the existing API client) to keep the established control paths small.
The unchanged hello capability uses `pageSize:20`; amended catalog replies use
`truncated` and at most 100 events, with no cursor or transfer grant fields.


## Follow-up 3o-r2 offline validation

2026-11-05: The full suite using the established interpreter passed 960 tests
and 1,200 subtests, with no failures. Command (from this repository root):

```powershell
& 'C:/Users/jason/OneDrive/Documents/Cursor/eufy/contributions/owned-pr-reconciliation-20261005/ha/checkout/.venv/Scripts/python.exe' -B tools/run_offline_tests.py
```

The regression coverage accepts a growing unique camera roster, retains stable
folder identities, verifies trimmed folder titles with history notices in an
information child, and omits event type from the date-first event title. The
optional cached `recordings.diagnostics` RPC is projected into HA diagnostics
with only counts, display names, fixed statuses and masked time shapes. Raw
canaries and unmasked/oversized shapes are rejected or omitted. Opaque folder
links are retained under `recordings.folders`. An older bridge without that
RPC keeps the folder links.

Changed Python files passed the same environment's `python -m ruff check` and
`python -m ruff format --check`. Full log:
`C:/Dev/eufy-s1-rectest-20261102/artifacts/recordings-3o-r2-ha-full.log`.
No live HA, camera, station, SSH, cloud, credentials or private capture was read.
Frontend rendering and live paging remain separate lead acceptance checks.
