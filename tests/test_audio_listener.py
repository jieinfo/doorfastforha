"""Behavior tests for shared bounded audio with independent listeners."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import sys
import types
import unittest


COMPONENT = Path(__file__).parents[1] / "custom_components" / "doorfast"
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
if "custom_components.doorfast" not in sys.modules:
    package = types.ModuleType("custom_components.doorfast")
    package.__path__ = [str(COMPONENT)]
    sys.modules["custom_components.doorfast"] = package


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


client_types = sys.modules.get("custom_components.doorfast.client_types")
if client_types is None:
    client_types = load_module(
        "custom_components.doorfast.client_types", COMPONENT / "client_types.py"
    )
audio_module = load_module("custom_components.doorfast.audio", COMPONENT / "audio.py")
AudioListenerHub = getattr(audio_module, "AudioListenerHub", None)
AudioIdentity = client_types.AudioIdentity
AudioChunk = client_types.AudioChunk
AudioReadReply = client_types.AudioReadReply
AudioStreamError = client_types.AudioStreamError
IDENTITY = AudioIdentity("0123456789abcdef", "gate_main", 7)


def chunk_reply(previous, revision, timestamp=1000, latest=None, identity=IDENTITY):
    return AudioReadReply("chunk", AudioChunk(
        identity, previous, revision, b"\x12\x34" * 800, timestamp,
        timestamp if latest is None else latest, 0, False,
    ))


class FakeAudioClient:
    """Replace only the network boundary; count actual overlapping requests."""

    def __init__(self, replies, gate=None):
        self.replies = list(replies)
        self.gate = gate
        self.started = asyncio.Event()
        self.read_count = 0
        self.inflight = 0
        self.max_inflight = 0
        self.requests = []

    async def read_audio_chunk(self, identity, after=None):
        self.requests.append((identity, after))
        self.read_count += 1
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        finally:
            self.inflight -= 1


class AudioListenerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(AudioListenerHub, "AudioListenerHub is not implemented")
        self.now = 0.0

    def hub(self, replies, *listeners, gate=None):
        client = FakeAudioClient(replies, gate)
        hub = AudioListenerHub(client, IDENTITY, lambda: self.now)
        for listener in listeners:
            hub.add(listener)
        return hub, client

    async def queued_read(self, hub, listener):
        entered = asyncio.Event()

        async def read():
            entered.set()
            return await hub.read(listener)

        task = asyncio.create_task(read())
        await entered.wait()
        return task

    async def test_two_listeners_receive_same_chunk_without_two_fetches(self):
        hub, client = self.hub([chunk_reply(0, 1)], "first", "second")
        a = await hub.read("first")
        b = await hub.read("second")
        self.assertEqual(b"\x12\x34" * 800, a.chunk.pcm)
        self.assertEqual(1, b.chunk.revision)
        self.assertEqual(a.chunk.pcm, b.chunk.pcm)
        self.assertEqual(1, client.read_count)
        with self.assertRaises(FrozenInstanceError):
            a.chunk.discontinuity = True

    async def test_concurrent_listeners_share_single_inflight_fetch(self):
        gate = asyncio.Event()
        hub, client = self.hub([chunk_reply(0, 1)], "a", "b", gate=gate)
        a = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        b = await self.queued_read(hub, "b")
        self.assertFalse(b.done())
        self.assertEqual(1, client.read_count)
        gate.set()
        replies = await asyncio.gather(a, b)
        self.assertEqual([1, 1], [reply.chunk.revision for reply in replies])
        self.assertEqual(1, client.read_count)
        self.assertEqual(1, client.max_inflight)

    async def test_same_listener_reads_are_serial_and_advance_own_cursor(self):
        gate = asyncio.Event()
        hub, client = self.hub([chunk_reply(0, 1), chunk_reply(1, 2)], "a", gate=gate)
        a = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        b = await self.queued_read(hub, "a")
        self.assertFalse(b.done())
        gate.set()
        replies = await asyncio.gather(a, b)
        self.assertEqual([1, 2], [reply.chunk.revision for reply in replies])
        self.assertEqual([(IDENTITY, None), (IDENTITY, 1)], client.requests)
        self.assertEqual(1, client.max_inflight)

    async def test_empty_304_never_replays_pcm_and_cools_shared_pulls(self):
        hub, client = self.hub(
            [chunk_reply(0, 1), AudioReadReply("empty"), chunk_reply(1, 2)], "a", "b"
        )
        await hub.read("a")
        await hub.read("b")
        self.assertEqual(AudioReadReply("empty"), await hub.read("a"))
        self.assertEqual(AudioReadReply("empty"), await hub.read("b"))
        self.now = 0.049
        self.assertEqual("empty", (await hub.read("a")).state)
        self.assertEqual(2, client.read_count)
        self.now = 0.05
        self.assertEqual(2, (await hub.read("b")).chunk.revision)
        self.assertEqual(3, client.read_count)

    async def test_fifth_listener_rejected_and_existing_listener_preserved(self):
        hub, client = self.hub([chunk_reply(0, 1)], "a", "b", "c", "d")
        hub.add("a")
        with self.assertRaises(AudioStreamError) as caught:
            hub.add("e")
        self.assertEqual("audio_listener_limit", caught.exception.code)
        replies = [await hub.read(listener) for listener in ("a", "b", "c", "d")]
        self.assertEqual([1, 1, 1, 1], [reply.chunk.revision for reply in replies])
        self.assertEqual(1, client.read_count)

    async def test_cache_keeps_four_chunks_and_slow_listener_moves_independently(self):
        hub, client = self.hub([chunk_reply(i - 1, i) for i in range(1, 7)], "fast", "slow")
        await hub.read("fast")
        await hub.read("slow")
        for _ in range(4):
            await hub.read("fast")
        slow = await hub.read("slow")
        self.assertEqual(2, slow.chunk.revision)
        self.assertFalse(slow.chunk.discontinuity)
        await hub.read("fast")
        slow = await hub.read("slow")
        self.assertEqual(3, slow.chunk.revision)
        self.assertEqual(6, client.read_count)

    async def test_evicted_cursor_marks_only_slow_listener_discontinuous(self):
        hub, client = self.hub([chunk_reply(i - 1, i) for i in range(1, 7)], "fast", "slow")
        for _ in range(6):
            await hub.read("fast")
        slow = await hub.read("slow")
        self.assertEqual(3, slow.chunk.revision)
        self.assertTrue(slow.chunk.discontinuity)
        self.assertFalse(client.replies)

    async def test_local_cache_older_than_300ms_is_not_played(self):
        hub, client = self.hub([chunk_reply(0, 1), chunk_reply(1, 2)], "fast", "slow")
        await hub.read("fast")
        self.now = 0.301
        reply = await hub.read("slow")
        self.assertEqual(2, reply.chunk.revision)
        self.assertTrue(reply.chunk.discontinuity)
        self.assertEqual(2, client.read_count)

    async def test_bridge_timestamp_lag_drops_stale_speech_without_local_clock_mix(self):
        hub, client = self.hub([
            chunk_reply(0, 1, timestamp=900000, latest=900301),
            chunk_reply(1, 2, timestamp=900301, latest=900301),
        ], "a")
        self.assertEqual(AudioReadReply("empty"), await hub.read("a"))
        self.now = 0.05
        reply = await hub.read("a")
        self.assertEqual(2, reply.chunk.revision)
        self.assertTrue(reply.chunk.discontinuity)
        self.assertEqual([(IDENTITY, None), (IDENTITY, None)], client.requests)

    async def test_new_source_edge_expires_slow_listener_backlog(self):
        hub, _ = self.hub([
            chunk_reply(0, 1, timestamp=1000),
            chunk_reply(1, 2, timestamp=1400),
        ], "fast", "slow")
        await hub.read("fast")
        await hub.read("fast")
        slow = await hub.read("slow")
        self.assertEqual(2, slow.chunk.revision)
        self.assertTrue(slow.chunk.discontinuity)

    async def test_source_lag_and_cache_age_together_exceed_300ms(self):
        hub, client = self.hub([
            chunk_reply(0, 1, timestamp=900000, latest=900250),
            chunk_reply(1, 2, timestamp=900350, latest=900350),
        ], "fast", "slow")
        self.assertEqual(1, (await hub.read("fast")).chunk.revision)
        self.now = 0.1
        slow = await hub.read("slow")
        self.assertEqual(2, slow.chunk.revision)
        self.assertTrue(slow.chunk.discontinuity)
        self.assertEqual(2, client.read_count)

    async def test_backlogged_listener_jumps_latest_among_multiple_fresh_chunks(self):
        hub, client = self.hub([
            chunk_reply(0, 1, timestamp=1000),
            chunk_reply(1, 2, timestamp=1100),
            chunk_reply(2, 3, timestamp=1200),
            chunk_reply(3, 4, timestamp=1301),
            AudioReadReply("empty"),
        ], "fast", "slow", "other")
        await hub.read("fast")
        await hub.read("other")
        for _ in range(3):
            await hub.read("fast")
        slow = await hub.read("slow")
        self.assertEqual(4, slow.chunk.revision)
        self.assertTrue(slow.chunk.discontinuity)
        other = await hub.read("other")
        self.assertEqual(2, other.chunk.revision)
        self.assertFalse(other.chunk.discontinuity)
        self.assertEqual(4, client.read_count)
        self.assertEqual("empty", (await hub.read("fast")).state)
        self.assertEqual(4, client.requests[-1][1])

    async def test_backlogged_listener_does_not_play_latest_if_it_is_also_stale(self):
        hub, client = self.hub([
            chunk_reply(0, 1, timestamp=1000),
            chunk_reply(1, 2, timestamp=1100),
            chunk_reply(2, 3, timestamp=1200),
            chunk_reply(3, 4, timestamp=1301),
            AudioReadReply("empty"),
            chunk_reply(4, 5, timestamp=1400),
        ], "fast", "slow")
        for _ in range(4):
            await hub.read("fast")
        self.now = 0.301
        self.assertEqual(AudioReadReply("empty"), await hub.read("slow"))
        self.assertEqual(5, client.read_count)
        self.now = 0.352
        latest = await hub.read("slow")
        self.assertEqual(5, latest.chunk.revision)
        self.assertTrue(latest.chunk.discontinuity)

    async def test_resync_preserves_dedup_high_water_and_next_chunk_discontinuity(self):
        hub, client = self.hub([
            chunk_reply(0, 1), AudioReadReply("resync"), chunk_reply(0, 1),
            chunk_reply(1, 2),
        ], "a", "b")
        await hub.read("a")
        await hub.read("b")
        self.assertEqual(AudioReadReply("resync"), await hub.read("a"))
        self.assertEqual(AudioReadReply("empty"), await hub.read("b"))
        self.now = 0.05
        a = await hub.read("a")
        b = await hub.read("b")
        self.assertEqual([2, 2], [a.chunk.revision, b.chunk.revision])
        self.assertTrue(a.chunk.discontinuity)
        self.assertTrue(b.chunk.discontinuity)
        self.assertEqual([None, 1, None, 1], [after for _, after in client.requests])

    async def test_invalid_results_retry_until_five_second_deadline_then_close(self):
        hub, client = self.hub([
            AudioStreamError("audio_invalid_response"), AudioReadReply("retry"),
            TimeoutError(),
        ], "a")
        self.assertEqual(AudioReadReply("retry"), await hub.read("a"))
        self.now = 4.99
        self.assertEqual(AudioReadReply("retry"), await hub.read("a"))
        self.now = 5.0
        with self.assertRaises(AudioStreamError) as caught:
            await hub.read("a")
        self.assertEqual("audio_connection_lost", caught.exception.code)
        self.assertTrue(hub.closed)
        self.assertLessEqual(client.read_count, 3)

    async def test_valid_empty_results_do_not_hit_failure_deadline(self):
        hub, client = self.hub([AudioReadReply("empty")] * 7, "a")
        for second in range(7):
            self.now = float(second)
            self.assertEqual("empty", (await hub.read("a")).state)
        self.assertFalse(hub.closed)
        self.assertEqual(7, client.read_count)

    async def test_valid_reply_resets_failure_deadline(self):
        hub, _ = self.hub([
            AudioReadReply("retry"), AudioReadReply("empty"),
            AudioReadReply("retry"), AudioReadReply("retry"),
        ], "a")
        await hub.read("a")
        self.now = 4.0
        await hub.read("a")
        self.now = 6.0
        self.assertEqual("retry", (await hub.read("a")).state)
        self.now = 10.9
        self.assertEqual("retry", (await hub.read("a")).state)
        self.assertFalse(hub.closed)

    async def test_failure_deadline_includes_first_invalid_request_duration(self):
        gate = asyncio.Event()
        hub, client = self.hub([TimeoutError(), AudioReadReply("retry")], "a", gate=gate)
        pending = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        self.now = 2.0
        gate.set()
        self.assertEqual("retry", (await pending).state)
        self.now = 5.0
        with self.assertRaises(AudioStreamError) as caught:
            await hub.read("a")
        self.assertEqual("audio_connection_lost", caught.exception.code)
        self.assertEqual(1, client.read_count)

    async def test_identity_error_immediately_closes_hub(self):
        for result in (
            AudioStreamError("audio_identity_mismatch"),
            chunk_reply(0, 1, identity=AudioIdentity("0123456789abcdef", "other", 7)),
            chunk_reply(0, 1, identity=AudioIdentity("fedcba9876543210", "gate_main", 7)),
            chunk_reply(0, 1, identity=AudioIdentity("0123456789abcdef", "gate_main", 8)),
        ):
            hub, client = self.hub([result], "a")
            with self.assertRaises(AudioStreamError) as caught:
                await hub.read("a")
            self.assertEqual("audio_identity_mismatch", caught.exception.code)
            self.assertTrue(hub.closed)
            self.assertEqual(1, client.read_count)

    async def test_non_temporary_stream_error_is_preserved(self):
        hub, client = self.hub([AudioStreamError("invalid_request")], "a")
        with self.assertRaises(AudioStreamError) as caught:
            await hub.read("a")
        self.assertEqual("invalid_request", caught.exception.code)
        self.assertTrue(hub.closed)
        self.assertEqual(1, client.read_count)

    async def test_identity_exception_closes_hub_after_fetch_listener_is_reused(self):
        hub, client = self.hub([
            chunk_reply(0, 1), AudioStreamError("audio_identity_mismatch"),
        ], "a", "b")
        await hub.read("a")
        client.started.clear()
        gate = asyncio.Event()
        client.gate = gate
        pending = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        hub.remove("a")
        hub.add("a")
        gate.set()
        with self.assertRaises(AudioStreamError) as caught:
            await pending
        self.assertEqual("audio_identity_mismatch", caught.exception.code)
        self.assertTrue(hub.closed)
        self.assertEqual(0, len(hub._cache))
        self.assertEqual(0, len(hub._listeners))
        with self.assertRaises(AudioStreamError) as other:
            await hub.read("b")
        self.assertEqual("audio_listener_closed", other.exception.code)
        self.assertEqual(2, client.read_count)

    async def test_late_identity_error_from_closed_epoch_cannot_affect_replacement(self):
        gate = asyncio.Event()
        old, old_client = self.hub([AudioStreamError("audio_identity_mismatch")], "a", gate=gate)
        pending = asyncio.create_task(old.read("a"))
        await old_client.started.wait()
        old.close()
        replacement, new_client = self.hub([chunk_reply(0, 1)], "a", "b")
        await replacement.read("a")
        gate.set()
        with self.assertRaises(AudioStreamError) as caught:
            await pending
        self.assertEqual("audio_listener_closed", caught.exception.code)
        self.assertEqual(0, len(old._cache))
        self.assertFalse(replacement.closed)
        self.assertEqual(1, (await replacement.read("b")).chunk.revision)
        self.assertEqual(1, old_client.read_count)
        self.assertEqual(1, new_client.read_count)

    async def test_cancelled_fetch_does_not_poison_shared_lock(self):
        gate = asyncio.Event()
        hub, client = self.hub([chunk_reply(0, 1)], "a", "b", gate=gate)
        pending = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        gate.set()
        self.assertEqual(1, (await hub.read("b")).chunk.revision)
        self.assertFalse(hub.closed)
        self.assertEqual(1, client.max_inflight)

    async def test_close_invalidates_late_fetch_and_waiting_listener(self):
        gate = asyncio.Event()
        hub, client = self.hub([chunk_reply(0, 1)], "a", "b", gate=gate)
        a = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        b = await self.queued_read(hub, "b")
        self.assertFalse(b.done())
        hub.close()
        gate.set()
        results = await asyncio.gather(a, b, return_exceptions=True)
        self.assertEqual(["audio_listener_closed"] * 2, [result.code for result in results])
        self.assertTrue(hub.closed)
        self.assertEqual(0, len(hub._cache))
        self.assertEqual(1, client.read_count)
        self.assertEqual(1, client.max_inflight)

    async def test_remove_last_listener_closes_and_prevents_new_requests(self):
        hub, client = self.hub([], "a", "b")
        hub.remove("a")
        self.assertFalse(hub.closed)
        hub.remove("b")
        self.assertTrue(hub.closed)
        with self.assertRaises(AudioStreamError):
            await hub.read("b")
        self.assertEqual(0, client.read_count)

    async def test_remove_last_listener_invalidates_late_fetch(self):
        gate = asyncio.Event()
        hub, client = self.hub([chunk_reply(0, 1)], "a", gate=gate)
        pending = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        hub.remove("a")
        gate.set()
        with self.assertRaises(AudioStreamError) as caught:
            await pending
        self.assertEqual("audio_listener_closed", caught.exception.code)
        self.assertEqual(0, len(hub._cache))
        self.assertEqual(1, client.read_count)

    async def test_removed_listener_cannot_consume_late_result_after_id_reuse(self):
        gate = asyncio.Event()
        hub, client = self.hub([chunk_reply(0, 1)], "a", "b", gate=gate)
        old = asyncio.create_task(hub.read("a"))
        await client.started.wait()
        hub.remove("a")
        hub.add("a")
        gate.set()
        with self.assertRaises(AudioStreamError) as caught:
            await old
        self.assertEqual("audio_listener_not_found", caught.exception.code)
        self.assertEqual(1, (await hub.read("a")).chunk.revision)
        self.assertEqual(1, client.read_count)

    async def test_unregistered_listener_does_not_fetch(self):
        hub, client = self.hub([], "a")
        with self.assertRaises(AudioStreamError) as caught:
            await hub.read("unknown")
        self.assertEqual("audio_listener_not_found", caught.exception.code)
        self.assertEqual(0, client.read_count)

if __name__ == "__main__":
    unittest.main()
