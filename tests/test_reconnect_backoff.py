"""Exercise local reconnect scheduling without transport I/O or real sleeps."""

# ruff: noqa: ANN002, ANN003, ANN201, ANN202, D102, INP001, PT009, SLF001

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.eufy_sdk.api import (
    EufySdkApiClient,
    EufySdkApiClientCommunicationError,
)


class ReconnectBackoffTests(unittest.IsolatedAsyncioTestCase):
    """Retain one supervisor, connect serialization and exponential retry."""

    async def test_sleep_sequence_and_success_callback(self):
        callback = Mock()
        client = EufySdkApiClient(
            "synthetic-bridge", 3000, Mock(), on_reconnect=callback
        )
        client.connect = AsyncMock(
            side_effect=[EufySdkApiClientCommunicationError("synthetic")] * 5 + [None]
        )
        with patch(
            "custom_components.eufy_sdk.api.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            await client._reconnect()
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list], [1, 2, 4, 5, 5]
        )
        self.assertEqual(client.connect.await_count, 6)
        callback.assert_called_once_with()

    async def test_single_supervisor_and_close_stops_retries(self):
        client = EufySdkApiClient("synthetic-bridge", 3000, Mock())
        entered = asyncio.Event()
        released = asyncio.Event()

        async def connect():
            entered.set()
            await released.wait()

        client.connect = AsyncMock(side_effect=connect)
        client._schedule_reconnect()
        supervisor = client._reconnect_task
        await entered.wait()
        client._schedule_reconnect()
        self.assertIs(client._reconnect_task, supervisor)
        await client.close()
        await asyncio.gather(supervisor, return_exceptions=True)
        self.assertTrue(supervisor.cancelled())
        self.assertIsNone(client._reconnect_task)
        client.connect.assert_awaited_once()

    async def test_concurrent_connects_share_the_existing_lock(self):
        entered, released = asyncio.Event(), asyncio.Event()
        socket = Mock(closed=False, close=AsyncMock())

        async def ws_connect(*_args, **_kwargs):
            entered.set()
            await released.wait()
            return socket

        session = Mock(ws_connect=AsyncMock(side_effect=ws_connect))
        client = EufySdkApiClient("synthetic-bridge", 3000, session)
        client._receive_loop = AsyncMock()
        first = asyncio.create_task(client.connect())
        await entered.wait()
        second = asyncio.create_task(client.connect())
        released.set()
        await asyncio.gather(first, second)
        session.ws_connect.assert_awaited_once()
        await client.close()
