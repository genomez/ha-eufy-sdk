"""Exercise persistent command retries and final dispatch admission."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, D102, FBT003
# ruff: noqa: INP001, PT009, SLF001
import asyncio
import unittest

import test_light_correction as fixtures


class CommandSafetyTests(fixtures.SafetyFixture, unittest.IsolatedAsyncioTestCase):
    """Use real entity requests and the public safety lifecycle."""

    async def test_off_retries_continue_beyond_old_three_attempt_limit(self):
        entity, _, _, _ = await self.activate()
        await entity.write(False)
        await self.advance(1080)
        self.assertEqual(
            [at for at, _, _ in self.commands], [0, 60, 180, 480, 780, 1080]
        )
        self.assertTrue(all(value is False for _, value, _ in self.commands))

    async def test_explicit_on_retry_backoff_and_off_supersession(self):
        entity, _, _, _ = await self.activate(desired=True)
        await entity.write(True)
        await self.advance(510)
        self.assertEqual([at for at, _, _ in self.commands], [0, 30, 90, 210, 510])
        await entity.write(False)
        await self.advance(1000)
        self.assertTrue(all(value is False for _, value, _ in self.commands[5:]))

    async def test_camera_match_stops_on_retry_without_reauthorizing_later_on(self):
        entity, _, _, _ = await self.activate(desired=True)
        await entity.write(True)
        self.observation(True)
        self.observation(False)
        await self.advance(600)
        self.assertEqual([value for _, value, _ in self.commands], [True])

    async def test_matching_bounded_read_suppresses_duplicate_on(self):
        entity, _, client, _ = await self.activate(desired=True)
        await entity.write(True)
        client.confirm_light.side_effect = lambda _sn, intent: {
            "intent": intent,
            "confirmed": True,
            "observedValue": True,
            "recoveryBusy": False,
        }
        await self.advance(120)
        self.assertEqual([value for _, value, _ in self.commands], [True])
        client.confirm_light.assert_awaited()
        self.assertIs(entity._light_correction.pending, True)

    async def test_only_one_automatic_attempt_outstanding(self):
        entity, _, client, _ = await self.activate()
        await entity.write(False)
        send = client.set_property.side_effect
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await send(*args, **kwargs)

        client.set_property.side_effect = delayed
        await self.advance(60)
        self.assertTrue(entered.is_set())
        await self.advance(600)
        self.assertEqual(client.set_property.await_count, 2)
        release.set()
        await self.drain()
        self.assertEqual(len(self.commands), 2)
        self.assertIsNone(entity._light_correction.task)

    async def test_pending_sdk_recovery_blocks_retries_until_final_read(self):
        entity, _, client, _ = await self.activate(desired=True)
        client.set_property.side_effect = None
        client.set_property.return_value = fixtures.CommandReceipt(
            "pending_accepted", True, "unconfirmed", intent=1
        )
        await entity.write(True)
        client.confirm_light.side_effect = lambda _sn, intent: {
            "intent": intent,
            "confirmed": False,
            "recoveryBusy": True,
        }
        await self.advance(120)
        self.assertEqual(client.set_property.await_count, 1)
        client.confirm_light.side_effect = lambda _sn, intent: {
            "intent": intent,
            "confirmed": False,
            "recoveryBusy": False,
        }
        entity._light_correction.check_policy()
        await self.drain()
        self.assertEqual(client.set_property.await_count, 2)

    async def test_intent_flip_during_capability_read_blocks_final_dispatch(self):
        entity, _, client, _ = await self.activate()
        await entity.write(False)
        self.binding("off")

        async def flip(_sn):
            self.states["sensor.fixture_intent"].state = "on"
            return [{"name": "light", "boundedStateWrites": True}]

        client.get_properties.side_effect = flip
        await self.advance(60)
        self.assertEqual([value for _, value, _ in self.commands], [False])
        self.assertFalse(entity._light_correction.on_authorized)

    async def test_explicit_request_is_never_delayed_by_controller_queue(self):
        entity, _, _, _ = await self.activate()
        queue = entity._light_correction.queue
        await queue.lock.acquire()
        try:
            await entity.write(True)
            self.assertEqual([value for _, value, _ in self.commands], [True])
        finally:
            queue.lock.release()
