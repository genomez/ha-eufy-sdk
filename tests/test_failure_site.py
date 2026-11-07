"""failure_site names the validating caller, not the shared field helper."""

# ruff: noqa: D103, INP001, S101

from __future__ import annotations

import pytest

from custom_components.eufy_sdk import recordings_models as models
from custom_components.eufy_sdk.media_source import failure_site


def _caller(source: str) -> object:
    namespace = {"models": models}
    path = "custom_components/eufy_sdk/fake_caller.py"
    exec(compile(source, path, "exec"), namespace)  # noqa: S102
    return namespace["caller"]


def test_number_failure_reports_caller_line_and_helper() -> None:
    caller = _caller("def caller():\n    x = 1\n    models.number(-1)\n")
    with pytest.raises(models.RecordingsError) as err:
        caller()
    assert failure_site(err.value) == "caller:3 via number"


def test_require_failure_reports_caller_line_only() -> None:
    caller = _caller("def caller():\n    models.require(False)\n")
    with pytest.raises(models.RecordingsError) as err:
        caller()
    assert failure_site(err.value) == "caller:2"


def test_real_validator_names_its_own_line() -> None:
    with pytest.raises(models.RecordingsError) as err:
        models.diagnostics({"version": 1, "lastPageCount": 21})
    site = failure_site(err.value)
    assert site.startswith("diagnostics:")
    assert not site.startswith("number:")
