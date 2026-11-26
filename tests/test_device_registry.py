"""Entry-scoped lookups support current and older Home Assistant registries."""

# ruff: noqa: INP001, S101

from types import SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.eufy_sdk.device_registry import async_entry_device


def test_modern_lookup_is_scoped_to_identifier_and_config_entry() -> None:
    """Pass both scope arguments to the HA 2026.11 lookup."""
    selected = object()
    registry = SimpleNamespace(
        async_get_device_by_identifier=Mock(return_value=selected)
    )
    with patch(
        "custom_components.eufy_sdk.device_registry.dr.async_get", return_value=registry
    ):
        assert (
            async_entry_device(Mock(), "synthetic-device", "synthetic-entry")
            is selected
        )
    registry.async_get_device_by_identifier.assert_called_once_with(
        ("eufy_sdk", "synthetic-device"), "synthetic-entry"
    )


def test_older_registry_uses_entry_enumeration_without_deprecated_lookup() -> None:
    """Find only an exact identifier among the owning entry's devices."""
    selected = SimpleNamespace(identifiers={("eufy_sdk", "synthetic-device")})
    other = SimpleNamespace(identifiers={("eufy_sdk", "other-device")})
    registry = SimpleNamespace(async_get_device=Mock(side_effect=AssertionError))
    with (
        patch(
            "custom_components.eufy_sdk.device_registry.dr.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.eufy_sdk.device_registry.dr.async_entries_for_config_entry",
            return_value=[other, selected],
        ) as entries,
    ):
        assert (
            async_entry_device(Mock(), "synthetic-device", "synthetic-entry")
            is selected
        )
        entries.assert_called_once_with(registry, "synthetic-entry")
        assert async_entry_device(Mock(), "absent-device", "synthetic-entry") is None
    registry.async_get_device.assert_not_called()


def test_missing_entry_never_resolves_another_entries_device() -> None:
    """Refuse an unscoped lookup before consulting the registry."""
    with patch("custom_components.eufy_sdk.device_registry.dr.async_get") as registry:
        assert async_entry_device(Mock(), "synthetic-device", None) is None
        registry.assert_not_called()
