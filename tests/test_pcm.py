"""Pure tests for the generation-bound Doorfast PCM producer."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
client_module = load_module("custom_components.doorfast.client_types", COMPONENT / "client_types.py")
pcm_module = load_module("custom_components.doorfast.pcm", COMPONENT / "pcm.py")
PcmHttpReply = client_module.PcmHttpReply
PcmProducer = pcm_module.PcmProducer
PcmProducerError = pcm_module.PcmProducerError
PcmProducerState = pcm_module.PcmProducerState

RUNTIME = "0123456789abcdef"
TOKEN = "a" * 32
FRAME = bytes(320)


def status(generation=7, runtime=RUNTIME):
    return {
        "runtime_id": runtime,
        "call": {"state": "talking", "generation": generation},
        "audio_tx": {"active": True, "generation": generation},
    }


def success(**changes):
    body = {
        "status": "success",
        "runtime_id": RUNTIME,
        "generation": 7,
        "audio_session": "",
        "accepted_frames": 0,
        "next_sequence": 0,
        "lease_ms": 2000,
    }
    body.update(changes)
    return PcmHttpReply(200, body)


def error(code, http=409, **changes):
    body = {
        "error": code,
        "runtime_id": RUNTIME,
        "generation": 7,
        "accepted_frames": 0,
        "next_sequence": 0,
    }
    body.update(changes)
    return PcmHttpReply(http, body)


class FakeClient:
    def __init__(self, replies, statuses=None, block_submit=None):
        self.status = status()
        self.replies = list(replies)
        self.statuses = list(statuses or [])
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.block_submit = block_submit

    async def refresh(self):
        self.calls.append(("refresh",))
        if self.statuses:
            value = self.statuses.pop(0)
            if isinstance(value, BaseException):
                raise value
            self.status = value
        return self.status

    async def _reply(self):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            value = self.replies.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        finally:
            self.in_flight -= 1

    async def pcm_session_open(self, runtime, generation):
        self.calls.append(("open", runtime, generation))
        return await self._reply()

    async def pcm_submit(self, runtime, generation, sequence, token, body):
        self.calls.append(("submit", runtime, generation, sequence, token, body))
        if self.block_submit is not None:
            await self.block_submit()
        return await self._reply()

    async def pcm_session_end(self, runtime, generation, token):
        self.calls.append(("end", runtime, generation, token))
        return await self._reply()


async def started(client):
    producer = PcmProducer(client)
    await producer.start()
    return producer


class PcmProducerTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_opens_submits_bounded_batch_and_stops(self):
        client = FakeClient([
            success(audio_session=TOKEN, next_sequence=9),
            success(accepted_frames=2, next_sequence=11),
            success(next_sequence=11, lease_ms=0),
        ])
        producer = await started(client)

        outcome = await producer.submit([FRAME, b"\x01" * 320])
        await producer.stop()

        self.assertEqual(PcmProducerState.IDLE, producer.state)
        self.assertEqual((2, 11, True, False), (
            outcome.accepted_frames, outcome.next_sequence,
            outcome.complete, outcome.recovered,
        ))
        self.assertEqual(("open", RUNTIME, 7), client.calls[1])
        self.assertEqual(("submit", RUNTIME, 7, 9, TOKEN, FRAME + b"\x01" * 320), client.calls[2])
        self.assertEqual(("end", RUNTIME, 7, TOKEN), client.calls[3])
        self.assertNotIn(TOKEN, repr(producer))

    async def test_rejects_malformed_or_stale_status_before_open(self):
        malformed = [
            {},
            status(runtime="0123456789ABCDEf"),
            {**status(), "call": {"state": "ringing", "generation": 7}},
            {**status(), "audio_tx": {"active": False, "generation": 7}},
            {**status(), "audio_tx": {"active": True, "generation": 8}},
            {**status(), "call": {"state": "talking", "generation": True}},
        ]
        for bad in malformed:
            with self.subTest(bad=bad):
                client = FakeClient([], [bad])
                producer = PcmProducer(client)
                with self.assertRaises(PcmProducerError):
                    await producer.start()
                self.assertEqual(PcmProducerState.IDLE, producer.state)
                self.assertFalse(any(call[0] == "open" for call in client.calls))

    async def test_treats_zero_authoritative_generation_as_identity_change(self):
        reply = error("generation_mismatch", generation=0)
        producer = PcmProducer(FakeClient([reply]))
        with self.assertRaises(PcmProducerError) as caught:
            await producer.start()
        self.assertEqual("identity_changed", caught.exception.code)

    async def test_rejects_malformed_success_identity_token_and_ranges(self):
        replies = [
            success(audio_session="A" * 32),
            success(audio_session=TOKEN, generation=8),
            success(audio_session=TOKEN, next_sequence=-1),
            success(audio_session=TOKEN, lease_ms=True),
            PcmHttpReply(200, []),
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                producer = PcmProducer(FakeClient([reply]))
                with self.assertRaises(PcmProducerError):
                    await producer.start()
                self.assertEqual(PcmProducerState.IDLE, producer.state)

    async def test_serializes_submissions_to_one_request_in_flight(self):
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def block_first():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()

        client = FakeClient([
            success(audio_session=TOKEN),
            success(accepted_frames=1, next_sequence=1),
            success(accepted_frames=1, next_sequence=2),
        ], block_submit=block_first)
        producer = await started(client)
        first = asyncio.create_task(producer.submit([FRAME]))
        await first_entered.wait()
        second = asyncio.create_task(producer.submit([b"\x02" * 320]))
        await asyncio.sleep(0)
        self.assertEqual(1, len([call for call in client.calls if call[0] == "submit"]))
        release_first.set()
        await asyncio.gather(first, second)
        self.assertEqual(1, client.max_in_flight)
        sequences = [call[3] for call in client.calls if call[0] == "submit"]
        self.assertEqual([0, 1], sequences)

    async def test_partial_503_advances_cursor_and_discards_unsent_suffix(self):
        client = FakeClient([
            success(audio_session=TOKEN, next_sequence=4),
            error("send_unavailable", 503, accepted_frames=2, next_sequence=6),
            success(accepted_frames=1, next_sequence=7),
        ])
        producer = await started(client)

        outcome = await producer.submit([b"a" * 320, b"b" * 320, b"c" * 320])
        next_outcome = await producer.submit([b"n" * 320])

        self.assertEqual((2, 6, False, True), (
            outcome.accepted_frames, outcome.next_sequence,
            outcome.complete, outcome.recovered,
        ))
        submits = [call for call in client.calls if call[0] == "submit"]
        self.assertEqual([4, 6], [call[3] for call in submits])
        self.assertEqual(b"n" * 320, submits[1][5])
        self.assertTrue(next_outcome.complete)

    async def test_lost_response_never_resends_old_bytes(self):
        client = FakeClient([
            success(audio_session=TOKEN, next_sequence=3),
            OSError("response lost"),
            error("sequence_duplicate", next_sequence=4),
            success(accepted_frames=1, next_sequence=5),
        ])
        producer = await started(client)

        with self.assertRaises(PcmProducerError) as caught:
            await producer.submit([b"o" * 320])
        self.assertEqual("response_lost", caught.exception.code)
        reconciled = await producer.submit([b"p" * 320])
        delivered = await producer.submit([b"q" * 320])

        submits = [call for call in client.calls if call[0] == "submit"]
        self.assertEqual([b"o" * 320, b"p" * 320, b"q" * 320], [call[5] for call in submits])
        self.assertEqual([3, 3, 4], [call[3] for call in submits])
        self.assertTrue(reconciled.recovered)
        self.assertFalse(reconciled.complete)
        self.assertTrue(delivered.complete)

    async def test_state_unavailable_non_durable_cursor_reconciles_gap_without_replay(self):
        client = FakeClient([
            success(audio_session=TOKEN, next_sequence=5),
            error("state_unavailable", 503, accepted_frames=1, next_sequence=6),
            error("sequence_gap", next_sequence=5),
            success(accepted_frames=1, next_sequence=6),
        ])
        producer = await started(client)

        await producer.submit([b"a" * 320, b"b" * 320])
        await producer.submit([b"c" * 320])
        await producer.submit([b"d" * 320])

        submits = [call for call in client.calls if call[0] == "submit"]
        self.assertEqual([5, 6, 5], [call[3] for call in submits])
        self.assertEqual([b"a" * 320 + b"b" * 320, b"c" * 320, b"d" * 320], [call[5] for call in submits])

    async def test_generation_change_during_recovery_ends_producer(self):
        client = FakeClient([
            success(audio_session=TOKEN),
            error("send_unavailable", 503),
        ], statuses=[status(), status(generation=8)])
        producer = await started(client)

        with self.assertRaises(PcmProducerError) as caught:
            await producer.submit([FRAME])
        self.assertEqual("identity_changed", caught.exception.code)
        self.assertEqual(PcmProducerState.IDLE, producer.state)

    async def test_session_expiry_reopens_once_and_discards_failed_body(self):
        new_token = "b" * 32
        client = FakeClient([
            success(audio_session=TOKEN, next_sequence=2),
            error("session_expired", next_sequence=2),
            success(audio_session=new_token, next_sequence=2),
            success(accepted_frames=1, next_sequence=3),
        ])
        producer = await started(client)

        expired = await producer.submit([b"x" * 320])
        accepted = await producer.submit([b"y" * 320])

        self.assertTrue(expired.recovered)
        self.assertFalse(expired.complete)
        submits = [call for call in client.calls if call[0] == "submit"]
        self.assertEqual([b"x" * 320, b"y" * 320], [call[5] for call in submits])
        self.assertEqual(new_token, submits[1][4])
        self.assertTrue(accepted.complete)

    async def test_busy_does_not_retry_or_spin(self):
        client = FakeClient([error("producer_busy")])
        producer = PcmProducer(client)
        with self.assertRaises(PcmProducerError) as caught:
            await producer.start()
        self.assertEqual("producer_busy", caught.exception.code)
        self.assertEqual(2, len(client.calls))
        self.assertEqual(PcmProducerState.IDLE, producer.state)

    async def test_rejects_unknown_error_category(self):
        client = FakeClient([success(audio_session=TOKEN), error("unexpected_server_story")])
        producer = await started(client)
        with self.assertRaises(PcmProducerError) as caught:
            await producer.submit([FRAME])
        self.assertEqual("invalid_response", caught.exception.code)
        self.assertEqual(PcmProducerState.IDLE, producer.state)

    async def test_malformed_error_is_rejected_without_cursor_change(self):
        client = FakeClient([
            success(audio_session=TOKEN, next_sequence=2),
            error("sequence_duplicate", next_sequence=True),
        ])
        producer = await started(client)
        with self.assertRaises(PcmProducerError) as caught:
            await producer.submit([FRAME])
        self.assertEqual("invalid_response", caught.exception.code)
        self.assertEqual(PcmProducerState.IDLE, producer.state)

    async def test_rejects_sequence_overflow_without_sending_body(self):
        client = FakeClient([success(audio_session=TOKEN, next_sequence=(1 << 64) - 1)])
        producer = await started(client)
        with self.assertRaises(PcmProducerError) as caught:
            await producer.submit([FRAME])
        self.assertEqual("sequence_overflow", caught.exception.code)
        self.assertFalse(any(call[0] == "submit" for call in client.calls))
        self.assertEqual(PcmProducerState.ACTIVE, producer.state)

    async def test_validates_frame_count_and_size_without_http(self):
        producer = await started(FakeClient([success(audio_session=TOKEN)]))
        for frames in ([], [b"x" * 319], [FRAME] * 6):
            with self.subTest(frames=len(frames)):
                with self.assertRaises(ValueError):
                    await producer.submit(frames)
        self.assertEqual(2, len(producer._client.calls))
        self.assertEqual(PcmProducerState.ACTIVE, producer.state)

    async def test_stop_is_best_effort_and_always_clears_private_state(self):
        client = FakeClient([success(audio_session=TOKEN), OSError("offline")])
        producer = await started(client)
        await producer.stop()
        await producer.stop()
        self.assertEqual(PcmProducerState.IDLE, producer.state)
        self.assertEqual(1, len([call for call in client.calls if call[0] == "end"]))
        self.assertNotIn(TOKEN, repr(producer))


if __name__ == "__main__":
    unittest.main()
