"""Use the native entity, reported state and fake writes to qualify mask changes."""

# ruff: noqa: INP001, SLF001, PT009, PT027

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from homeassistant.const import EntityCategory

from custom_components.eufy_sdk.switch import EufyBitmaskSwitch


def entity_for(value: object, *, bit: int = 4) -> EufyBitmaskSwitch:
    """Construct a native entity with synthetic coordinator state, never a bridge."""
    coordinator = SimpleNamespace(
        data={"synthetic-camera": {"state": {"aiDetectType": value}}},
    )
    entity = EufyBitmaskSwitch(
        coordinator,
        "synthetic-camera",
        {"name": "aiDetectType", "type": "number", "kind": "bitfield"},
        {"key": "detect_human", "bit": bit, "base": 0x30000},
    )
    entity.write = AsyncMock()
    return entity


class KnownMaskRegressionTests(IsolatedAsyncioTestCase):
    """Assertions deliberately fail when unchanged source injects fallback bits."""

    async def test_on_preserves_every_other_bit_including_reported_zero(self) -> None:
        """A known mask gains only the selected bit."""
        for mask in (0, 0x8003, 0x8007, 0x30003, (1 << 60) | 3):
            with self.subTest(mask=mask):
                entity = entity_for(mask)
                await entity.async_turn_on()
                entity.write.assert_awaited_once_with(mask | 4)
                self.assertEqual(entity.device["state"]["aiDetectType"], mask)

    async def test_off_preserves_every_other_bit_including_reported_zero(self) -> None:
        """A known mask loses only the selected bit."""
        for mask in (0, 0x8003, 0x8007, 0x30007, (1 << 60) | 7):
            with self.subTest(mask=mask):
                entity = entity_for(mask)
                await entity.async_turn_off()
                entity.write.assert_awaited_once_with(mask & ~4)
                self.assertEqual(entity.device["state"]["aiDetectType"], mask)

    async def test_selected_bit_in_fallback_range_can_be_cleared(self) -> None:
        """Known masks are authoritative even when a selected bit overlaps base."""
        entity = entity_for(0x30007, bit=0x10000)
        await entity.async_turn_off()
        entity.write.assert_awaited_once_with(0x20007)

    async def test_current_property_is_read_again_for_each_write(self) -> None:
        """Later reported unrelated bits survive the next write."""
        entity = entity_for(0x8007)
        await entity.async_turn_off()
        entity.write.assert_awaited_once_with(0x8003)
        entity.write.reset_mock()
        entity.device["state"]["aiDetectType"] = 0x40003
        await entity.async_turn_on()
        entity.write.assert_awaited_once_with(0x40007)

    async def test_coercible_values_write_without_base_injection(self) -> None:
        """Retained numeric coercion now changes only the requested bit on writes."""
        for value in (True, False, 32775.9, -1, -1.9):
            with self.subTest(value=value):
                entity = entity_for(value)
                await entity.async_turn_on()
                entity.write.assert_awaited_once_with(int(value) | 4)
                entity.write.reset_mock()
                await entity.async_turn_off()
                entity.write.assert_awaited_once_with(int(value) & ~4)


class ExistingMaskCompatibilityTests(IsolatedAsyncioTestCase):
    """Qualify retained coercion/fallback and presentation without expanding policy."""

    async def test_unknown_values_keep_fixed_fallback(self) -> None:
        """Unknown/string/object values use fallback, not reported-mask preservation."""
        for value in (None, "32775", "invalid", {}, []):
            with self.subTest(value=value):
                entity = entity_for(value)
                self.assertEqual(entity._mask(), 0x30000)
                await entity.async_turn_on()
                entity.write.assert_awaited_once_with(0x30004)
                entity.write.reset_mock()
                await entity.async_turn_off()
                entity.write.assert_awaited_once_with(0x30000)

    async def test_bool_float_and_negative_conversion_is_unchanged(self) -> None:
        """Existing int conversion accepts booleans and truncates fractional numbers."""
        for value in (True, False, 32775.9, -1, -1.9):
            with self.subTest(value=value):
                entity = entity_for(value)
                self.assertEqual(entity._mask(), int(value))

    async def test_nonfinite_float_still_raises_before_write(self) -> None:
        """No new admission or silent repair of existing nonfinite values occurs."""
        for value, error in ((float("nan"), ValueError), (float("inf"), OverflowError)):
            with self.subTest(value=value):
                entity = entity_for(value)
                with self.assertRaises(error):
                    await entity.async_turn_on()
                with self.assertRaises(error):
                    await entity.async_turn_off()
                entity.write.assert_not_awaited()

    async def test_unknown_display_and_entity_identity_are_unchanged(self) -> None:
        """Preserve unknown presentation, translation and stable per-bit identity."""
        entity = entity_for(None)
        self.assertIsNone(entity.is_on)
        self.assertEqual(entity.unique_id, "synthetic-camera_aiDetectType_4")
        self.assertEqual(entity.translation_key, "detect_human")
        self.assertEqual(entity.entity_category, EntityCategory.CONFIG)
        entity.device["state"]["aiDetectType"] = "4"
        self.assertTrue(entity.is_on)
        entity.device["state"]["aiDetectType"] = "invalid"
        with self.assertRaises(ValueError):
            _ = entity.is_on
