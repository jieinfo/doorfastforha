"""Synthetic HTTP boundaries exercise real hub/producer/WS lifecycle behavior."""
import asyncio
import base64
import gc
import unittest
from unittest.mock import patch

from test_audio_client import DoorfastClient
from test_pcm_websocket import Connection, Hass, ws

from custom_components.doorfast.client_types import AudioChunk, AudioIdentity, AudioReadReply, AudioStreamError, PcmHttpReply

RUNTIME = "0123456789abcdef"


def status(runtime=RUNTIME, generation=7):
    return {"runtime_id": runtime,
            "call": {"session": "talking", "station_id": "gate_main", "generation": generation},
            "audio": {"pcm_stream_version": 1},
            "audio_tx": {"active": True, "generation": generation}}


class Client:
    def __init__(self):
        self.status = status()
        self.reads = []
        self.opens = []
        self.ends = []
        self.open_entered = asyncio.Event()
        self.open_wait = None
        self.end_wait = None
        self.read_wait = None
        self.read_entered = asyncio.Event()
        self.active_reads = 0
        self.max_reads = 0
        self.refresh_wait = None

    async def refresh(self):
        if self.refresh_wait: await self.refresh_wait.wait()
        return self.status

    async def read_audio_chunk(self, identity, after=None):
        self.active_reads += 1
        self.max_reads = max(self.max_reads, self.active_reads)
        self.read_entered.set()
        try:
            if self.read_wait: await self.read_wait.wait()
            reply = self.reads.pop(0) if self.reads else AudioReadReply("empty")
            if isinstance(reply, Exception): raise reply
            return reply
        finally:
            self.active_reads -= 1

    def pcm_reply(self, runtime, generation, ending=False):
        return PcmHttpReply(200, {"status": "success", "runtime_id": runtime,
            "generation": generation, "audio_session": "" if ending else "a" * 32,
            "accepted_frames": 0, "next_sequence": 0, "lease_ms": 0 if ending else 2000})

    async def pcm_session_open(self, runtime, generation):
        self.opens.append((runtime, generation)); self.open_entered.set()
        if self.open_wait: await self.open_wait.wait()
        return self.pcm_reply(runtime, generation)

    async def pcm_session_end(self, runtime, generation, token):
        self.ends.append((runtime, generation))
        if self.end_wait: await self.end_wait.wait()
        return self.pcm_reply(runtime, generation, True)


class SubConnection(Connection):
    def __init__(self, admin=True):
        super().__init__(admin); self.subscriptions = {}; self.close_finished = False

    async def disconnect(self):
        # HA 2024.11.0 connection.async_handle_close uses a live values view.
        for cb in self.subscriptions.values():
            try:
                cb()
            except Exception:
                pass  # HA logs callback exceptions and continues iteration.
        self.subscriptions.clear()
        self.close_finished = True
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class Timer:
    def __init__(self, callback, args): self.callback=callback; self.args=args; self.cancelled=False
    def cancel(self): self.cancelled=True
    def fire(self):
        if not self.cancelled: self.callback(*self.args)


class GatedRefreshClient(DoorfastClient):
    """Real refresh sequence/cache semantics with only HTTP requests replaced."""
    def __init__(self, responses):
        super().__init__(None, "http://synthetic.invalid")
        self.status=status()
        self.responses=list(responses)

    async def _request(self, method, path, payload=None):
        response, entered, resume=self.responses.pop(0)
        entered.set()
        await resume.wait()
        return response


class AudioWebsocketTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=Client(); self.hass=Hass()
        self.hass.data["doorfast"]={"one": self.client, "two": Client()}
        self.hass.async_create_task=asyncio.create_task
        self.manager=ws.PcmWebSocketManager(self.hass); self.connection=SubConnection()
        self.timers=[]
        loop=asyncio.get_running_loop()
        def later(delay, callback, *args, **kwargs):
            self.assertEqual(5.0, delay)
            timer=Timer(callback,args); self.timers.append(timer); return timer
        self.timer_patch=patch.object(loop,"call_later",later); self.timer_patch.start()

    async def asyncTearDown(self):
        self.timer_patch.stop()
        await self.manager.release_entry("one"); await self.manager.release_entry("two")

    async def listen(self, connection=None, **extra):
        c=connection or self.connection
        await self.manager.listen_start(c,{"id":1,"config_entry_id":"one","station_id":"gate_main",**extra})
        self.assertFalse(c.errors)
        return c.results[-1][1]["listener_id"]

    async def read(self, lid, connection=None, **extra):
        c=connection or self.connection
        await self.manager.listen_read(c,{"id":2,"config_entry_id":"one","listener_id":lid,**extra})
        return c

    async def mic(self, lid):
        await self.manager.start(self.connection,{"id":3,"config_entry_id":"one","station_id":"gate_main","listener_id":lid})

    async def test_authenticated_chunk_contract(self):
        lid=await self.listen(generation=7)
        self.assertEqual({"listener_id":lid,"station_id":"gate_main","generation":7,
            "sample_rate":8000,"channels":1,"sample_format":"s16le"},self.connection.results[-1][1])
        self.client.reads=[AudioReadReply("chunk",AudioChunk(AudioIdentity(RUNTIME,"gate_main",7),0,1,b"\x01\x02",1,1,0,False))]
        await self.read(lid)
        self.assertEqual({"state":"chunk","listener_id":lid,"generation":7,"previous_revision":0,"revision":1,
            "pcm":base64.b64encode(b"\x01\x02").decode(),"discontinuity":False},self.connection.results[-1][1])
        self.assertFalse(self.client.opens)

    async def test_station_release_requires_exact_runtime_station_and_control_generation(self):
        lid = await self.listen()
        await self.mic(lid)
        for identity in ((RUNTIME, "gate_side", 7), (RUNTIME, "gate_main", 6),
                         ("fedcba9876543210", "gate_main", 7)):
            await self.manager.release_call("one", *identity)
            self.assertIn(lid, self.manager._listeners)
            self.assertIn("one", self.manager._by_entry)
        await self.manager.release_call("one", RUNTIME, "gate_main", 7)
        self.assertNotIn(lid, self.manager._listeners)
        self.assertNotIn("one", self.manager._by_entry)
        self.assertEqual([(RUNTIME, 7)], self.client.ends)

    async def test_admin_and_entry_connection_ownership(self):
        other=SubConnection(False)
        await self.manager.listen_start(other,{"id":1,"config_entry_id":"one","station_id":"gate_main"})
        self.assertEqual("unauthorized",other.errors[-1][1])
        lid=await self.listen()
        for method in (self.manager.listen_read,self.manager.listen_stop,self.manager.start):
            other=SubConnection()
            await method(other,{"id":2,"config_entry_id":"one","listener_id":lid})
            self.assertEqual("not_found",other.errors[-1][1])
            await method(self.connection,{"id":2,"config_entry_id":"two","listener_id":lid})
            self.assertEqual("not_found",self.connection.errors[-1][1]); self.connection.errors.clear()
        await self.read(lid); self.assertEqual({"state":"empty"},self.connection.results[-1][1])

    async def test_four_listener_limit_and_fresh_hub_after_last_stop(self):
        connections = [SubConnection() for _ in range(4)]
        ids=[await self.listen(c) for c in connections]
        fifth = SubConnection()
        await self.manager.listen_start(fifth,{"id":5,"config_entry_id":"one","station_id":"gate_main"})
        self.assertEqual("audio_listener_limit",fifth.errors[-1][1])
        for connection, lid in zip(connections, ids):
            await self.manager.listen_stop(connection,{"id":6,"config_entry_id":"one","listener_id":lid})
        lid=await self.listen(); await self.read(lid)
        self.assertEqual({"state":"empty"},self.connection.results[-1][1])

    async def test_duplicate_listener_active_is_rejected_for_same_connection_entry_station(self):
        await self.listen()
        await self.manager.listen_start(self.connection,{"id":5,"config_entry_id":"one","station_id":"gate_main"})
        self.assertTrue(self.connection.errors, "duplicate active start must fail")
        self.assertEqual("audio_listener_busy", self.connection.errors[-1][1])

    async def test_duplicate_listener_pending_is_rejected_before_second_refresh(self):
        self.client.refresh_wait = asyncio.Event()
        first_connection = SubConnection()
        first = asyncio.create_task(self.manager.listen_start(first_connection, {
            "id": 1, "config_entry_id": "one", "station_id": "gate_main",
        }))
        await asyncio.sleep(0)
        second = first_connection
        second_task = asyncio.create_task(self.manager.listen_start(second, {
            "id": 2, "config_entry_id": "one", "station_id": "gate_main",
        }))
        await asyncio.sleep(0)
        try:
            self.assertTrue(second_task.done(), "duplicate pending start must reject before refresh")
            self.assertEqual("audio_listener_busy", second.errors[-1][1])
        finally:
            self.client.refresh_wait.set()
            await asyncio.gather(first, second_task)

    async def test_js_generation_and_mandatory_station(self):
        for value in (True,1.5,0,9007199254740992):
            await self.manager.listen_start(self.connection,{"id":1,"config_entry_id":"one","station_id":"gate_main","generation":value})
            self.assertEqual("invalid_request",self.connection.errors[-1][1])
        for bad in ({}, {"station_id":""}):
            await self.manager.listen_start(self.connection,{"id":1,"config_entry_id":"one",**bad})
            self.assertTrue(self.connection.errors)
        self.client.status=status(generation=9007199254740992)
        await self.manager.listen_start(self.connection,{"id":1,"config_entry_id":"one","station_id":"gate_main"})
        self.assertEqual("invalid_request",self.connection.errors[-1][1])

    async def test_unsupported_capability_and_expected_generation(self):
        self.client.status["audio"]={}
        await self.manager.listen_start(self.connection,{"id":1,"config_entry_id":"one","station_id":"gate_main"})
        self.assertEqual("unsupported_audio_stream",self.connection.errors[-1][1])
        self.client.status=status()
        await self.manager.listen_start(self.connection,{"id":1,"config_entry_id":"one","station_id":"gate_main","generation":8})
        self.assertEqual("audio_identity_mismatch",self.connection.errors[-1][1])

    async def test_mic_bound_to_backend_runtime_and_busy(self):
        lid=await self.listen(); await self.mic(lid)
        self.assertEqual([(RUNTIME,7)],self.client.opens)
        await self.mic(lid); self.assertEqual("producer_busy",self.connection.errors[-1][1])
        self.connection.errors.clear(); other_connection=SubConnection(); other=await self.listen(other_connection)
        await self.manager.start(other_connection,{"id":3,"config_entry_id":"one","station_id":"gate_main","listener_id":other})
        self.assertEqual("producer_busy",other_connection.errors[-1][1])

    async def test_reused_generation_reboot_does_not_open_mic(self):
        lid=await self.listen(); self.client.status=status(runtime="fedcba9876543210")
        await self.mic(lid); self.assertEqual([],self.client.opens)
        self.assertEqual("runtime_mismatch",self.connection.errors[-1][1])

    async def test_bound_factory_typeerror_never_uses_legacy_fallback(self):
        lid=await self.listen(); calls=[]
        def factory(*args,**kwargs):
            calls.append((args,kwargs)); raise TypeError("private detail")
        self.manager.producer_factory=factory
        await self.mic(lid); self.assertEqual(1,len(calls))
        self.assertNotIn("private detail",str(self.connection.errors))

    async def test_stop_listener_preserves_other_listener(self):
        lid=await self.listen(); other_connection=SubConnection(); other=await self.listen(other_connection); await self.mic(lid)
        await self.manager.listen_stop(other_connection,{"id":4,"config_entry_id":"one","listener_id":other})
        self.assertFalse(self.client.ends)
        await self.read(lid); self.assertFalse(self.connection.errors)
        await self.manager.listen_stop(self.connection,{"id":4,"config_entry_id":"one","listener_id":lid})
        self.assertEqual([(RUNTIME,7)],self.client.ends); self.assertEqual({},self.connection.subscriptions)

    async def test_idle_lease_empty_refresh_and_expiry(self):
        lid=await self.listen(); first=self.timers[-1]; await self.mic(lid)
        await self.read(lid); self.assertTrue(first.cancelled)
        first.fire(); await asyncio.sleep(0); self.assertFalse(self.client.ends)
        self.timers[-1].fire(); await asyncio.sleep(0); await asyncio.sleep(0)
        await self.read(lid); self.assertEqual("not_found",self.connection.errors[-1][1])
        self.assertEqual([(RUNTIME,7)],self.client.ends); self.assertEqual({},self.connection.subscriptions)

    async def test_active_read_idle_expiry_discards_late_chunk(self):
        lid=await self.listen(); self.client.read_wait=asyncio.Event()
        task=asyncio.create_task(self.read(lid)); await asyncio.sleep(0)
        self.timers[-1].fire(); await asyncio.sleep(0); self.client.read_wait.set(); await task
        self.assertEqual("not_found",self.connection.errors[-1][1])

    async def test_downlink_failure_invalidates_shared_hub_and_linked_mic(self):
        lid=await self.listen(); other_connection=SubConnection(); other=await self.listen(other_connection); await self.mic(lid)
        self.client.reads=[AudioStreamError("audio_identity_mismatch")]
        await self.read(other, other_connection); self.assertEqual([(RUNTIME,7)],self.client.ends)
        await self.read(lid); self.assertEqual("not_found",self.connection.errors[-1][1])
        self.assertEqual({},self.connection.subscriptions)

    async def test_reconcile_restart_and_new_call_never_migrate_listeners(self):
        lid=await self.listen(); await self.mic(lid)
        self.client.status=status(runtime="fedcba9876543210")
        await self.manager.reconcile("one",self.client.status)
        await self.read(lid); self.assertEqual("not_found",self.connection.errors[-1][1]); self.connection.errors.clear()
        new=await self.listen(); self.assertNotEqual(lid,new)
        await self.read(new); self.assertFalse(self.connection.errors)

    async def test_fresh_start_invalidates_previous_identity(self):
        lid=await self.listen(); await self.mic(lid); self.client.status=status(generation=8)
        await self.listen(SubConnection()); await self.read(lid)
        self.assertEqual("not_found",self.connection.errors[-1][1]); self.assertEqual([(RUNTIME,7)],self.client.ends)

    async def test_release_entry_invalidates_before_blocked_end(self):
        lid=await self.listen(); await self.mic(lid); self.client.end_wait=asyncio.Event()
        task=asyncio.create_task(self.manager.release_entry("one")); await asyncio.sleep(0)
        await self.read(lid); self.assertEqual("not_found",self.connection.errors[-1][1])
        self.assertEqual({},self.connection.subscriptions)
        self.client.end_wait.set(); await task

    async def test_teardown_closes_admission_until_successful_setup_reopens_it(self):
        self.client.end_wait=asyncio.Event()
        try:
            lid=await self.listen(); await self.mic(lid)
            task=asyncio.create_task(self.manager.close_entry("one")); await asyncio.sleep(0)
            blocked = SubConnection()
            await self.manager.listen_start(blocked,{"id":5,"config_entry_id":"one","station_id":"gate_main"})
            self.assertEqual("not_loaded", blocked.errors[-1][1])
            self.client.end_wait.set(); await task
            await self.manager.listen_start(blocked,{"id":6,"config_entry_id":"one","station_id":"gate_main"})
            self.assertEqual("not_loaded", blocked.errors[-1][1])
            self.manager.admit_entry("one")
            blocked.errors.clear()
            await self.manager.listen_start(blocked,{"id":7,"config_entry_id":"one","station_id":"gate_main"})
            self.assertFalse(blocked.errors)
        finally:
            self.client.end_wait.set()

    async def test_late_open_is_closed_after_listener_stop(self):
        lid=await self.listen(); self.client.open_wait=asyncio.Event()
        task=asyncio.create_task(self.mic(lid)); await self.client.open_entered.wait()
        await self.manager.listen_stop(self.connection,{"id":4,"config_entry_id":"one","listener_id":lid})
        self.client.open_wait.set(); await task
        self.assertEqual([(RUNTIME,7)],self.client.ends)
        self.assertFalse(any("capture_id" in r for _,r in self.connection.results))

    async def test_disconnect_releases_and_prevents_late_open(self):
        lid=await self.listen(); self.client.open_wait=asyncio.Event()
        task=asyncio.create_task(self.mic(lid)); await self.client.open_entered.wait()
        await self.connection.disconnect(); self.client.open_wait.set(); await task
        self.assertEqual([(RUNTIME,7)],self.client.ends); self.assertEqual({},self.connection.subscriptions)

    async def test_native_live_subscription_close_cleans_all_callbacks(self):
        for pending_open in (False, True):
            self.connection = SubConnection()
            connection = self.connection
            lid = await self.listen()
            self.client.open_entered.clear()
            self.client.open_wait = asyncio.Event() if pending_open else None
            mic = asyncio.create_task(self.mic(lid))
            await self.client.open_entered.wait()
            if not pending_open: await mic
            unrelated = []
            def tail():
                self.assertFalse(self.manager._listeners)
                self.assertFalse(self.manager._captures)
                self.assertFalse(self.manager._pending_cleanups)
                unrelated.append(True)
            connection.subscriptions[("unrelated", "tail")] = tail
            try:
                await connection.disconnect()
                self.assertEqual([True], unrelated)
                self.assertTrue(connection.close_finished)
                self.assertEqual({}, connection.subscriptions)
            finally:
                if self.client.open_wait: self.client.open_wait.set()
                await mic
            self.assertFalse(self.manager._listener_reservations)

    async def test_fetch_is_serialized_across_hub_replacement(self):
        old_connection = SubConnection()
        old_id = await self.listen(old_connection)
        self.client.read_wait = asyncio.Event()
        old_read = asyncio.create_task(self.manager.listen_read(old_connection, {
            "id": 2, "config_entry_id": "one", "listener_id": old_id,
        }))
        await self.client.read_entered.wait()
        await self.manager.listen_stop(old_connection, {
            "id": 3, "config_entry_id": "one", "listener_id": old_id,
        })
        new_connection = SubConnection()
        new_id = await self.listen(new_connection)
        new_read = asyncio.create_task(self.manager.listen_read(new_connection, {
            "id": 4, "config_entry_id": "one", "listener_id": new_id,
        }))
        await asyncio.sleep(0)
        self.assertEqual(1, self.client.max_reads)
        self.client.read_wait.set()
        await old_read
        await new_read

    async def test_fetch_lock_does_not_retain_retired_entries(self):
        lid = await self.listen()
        await self.read(lid)
        await self.manager.release_entry("one")
        gc.collect()
        self.assertFalse(self.manager._fetch_locks, "idle retired hubs must not leak entry locks")

    async def test_removed_client_revokes_inflight_read_and_producer_start(self):
        for operation in ("read", "open"):
            self.connection = SubConnection()
            self.hass.data["doorfast"]["one"] = self.client
            lid = await self.listen()
            gate = asyncio.Event()
            if operation == "read":
                self.client.read_wait = gate
                self.client.read_entered.clear()
                task = asyncio.create_task(self.read(lid))
                await self.client.read_entered.wait()
            else:
                self.client.open_wait = gate
                self.client.open_entered.clear()
                task = asyncio.create_task(self.mic(lid))
                await self.client.open_entered.wait()
            self.hass.data["doorfast"].pop("one")
            gate.set()
            await task
            self.assertTrue(self.connection.errors, "client removal must invalidate pending operation")
            self.assertEqual("not_found", self.connection.errors[-1][1])
            self.assertFalse(self.manager._captures)
            await self.manager.release_entry("one")

    async def test_old_listener_pending_finally_cannot_release_replacement_reservation(self):
        entered_a, entered_b = asyncio.Event(), asyncio.Event()
        resume_a, resume_b = asyncio.Event(), asyncio.Event()
        client = GatedRefreshClient([(status(), entered_a, resume_a), (status(), entered_b, resume_b)])
        self.hass.data["doorfast"]["one"] = client
        message = {"id": 1, "config_entry_id": "one", "station_id": "gate_main"}
        old = asyncio.create_task(self.manager.listen_start(self.connection, message))
        await entered_a.wait()
        await self.manager.release_entry("one")
        new = asyncio.create_task(self.manager.listen_start(self.connection, {**message, "id": 2}))
        await entered_b.wait()
        try:
            resume_a.set()
            await old
            await self.manager.listen_start(self.connection, {**message, "id": 3})
            self.assertEqual("audio_listener_busy", self.connection.errors[-1][1])
        finally:
            resume_a.set(); resume_b.set()
            await asyncio.gather(old, new)
        self.assertEqual(1, len(self.manager._listeners))
        self.assertEqual(2, self.connection.results[-1][0])

    async def test_fallback_cleanup_removed_on_stop(self):
        c=Connection(); lid=await self.listen(c)
        await self.manager.listen_stop(c,{"id":2,"config_entry_id":"one","listener_id":lid})
        self.assertEqual([],c.cleanup)

    async def test_retry_resync_never_serialize_audio(self):
        for state in ("retry","resync"):
            lid=await self.listen(); self.client.reads=[AudioReadReply(state)]
            await self.read(lid); self.assertEqual({"state":"empty"},self.connection.results[-1][1])
            await self.manager.listen_stop(self.connection,{"id":3,"config_entry_id":"one","listener_id":lid})

    async def test_invalid_revision_never_reaches_browser(self):
        for revision in (True,1.5,-1,9007199254740992):
            lid=await self.listen()
            self.client.reads=[AudioReadReply("chunk",AudioChunk(AudioIdentity(RUNTIME,"gate_main",7),0,revision,b"\x00\x00",1,1,0,False))]
            await self.read(lid); self.assertFalse(any(r.get("state")=="chunk" for _,r in self.connection.results))
            self.connection.errors.clear()
            await self.manager.release_entry("one")

    async def test_pending_listen_start_revoked_during_refresh(self):
        for disconnect in (False, True):
            self.client.refresh_wait=asyncio.Event()
            c=SubConnection()
            task=asyncio.create_task(self.manager.listen_start(c,{"id":1,"config_entry_id":"one","station_id":"gate_main"}))
            await asyncio.sleep(0)
            if disconnect: await c.disconnect()
            else: await self.manager.release_entry("one")
            self.assertEqual({},c.subscriptions)
            self.client.refresh_wait.set(); await task
            self.assertEqual([],c.results); self.assertEqual("not_found",c.errors[-1][1])
            self.assertEqual({},c.subscriptions)
        self.client.refresh_wait=None

    async def test_failed_factory_does_not_keep_start_reservation(self):
        lid=await self.listen()
        original=self.manager.producer_factory
        def factory(*args,**kwargs): raise TypeError("failure")
        self.manager.producer_factory=factory
        await self.mic(lid); self.connection.errors.clear()
        self.manager.producer_factory=original
        await self.mic(lid)
        self.assertFalse(self.connection.errors)
        self.assertEqual([(RUNTIME,7)],self.client.opens)

    async def test_fallback_disconnect_waits_for_pcm_end_and_cleans_callbacks(self):
        c=Connection(); lid=await self.listen(c)
        await self.manager.start(c,{"id":2,"config_entry_id":"one","listener_id":lid})
        await c.disconnect()
        self.assertEqual([(RUNTIME,7)],self.client.ends)
        self.assertEqual([],c.cleanup)

    async def test_concurrent_read_rejected_without_releasing_owner(self):
        lid=await self.listen(); self.client.read_wait=asyncio.Event()
        task=asyncio.create_task(self.read(lid)); await asyncio.sleep(0)
        await self.read(lid); self.assertEqual("audio_read_busy",self.connection.errors[-1][1])
        await self.read(lid); self.assertEqual("audio_read_busy",self.connection.errors[-1][1])
        self.client.read_wait.set(); await task
        self.connection.errors.clear(); await self.read(lid)
        self.assertFalse(self.connection.errors)

    async def test_late_open_revoked_by_entry_release_or_reconcile(self):
        for reconcile in (False,True):
            self.connection=SubConnection()
            lid=await self.listen(); self.client.open_wait=asyncio.Event(); self.client.open_entered.clear()
            task=asyncio.create_task(self.mic(lid)); await self.client.open_entered.wait()
            if reconcile: await self.manager.reconcile("one",status(runtime="fedcba9876543210"))
            else: await self.manager.release_entry("one")
            self.assertEqual({},self.connection.subscriptions)
            self.client.open_wait.set(); await task
            self.assertFalse(any("capture_id" in result for _,result in self.connection.results))
        self.assertEqual([(RUNTIME,7),(RUNTIME,7)],self.client.ends)

    async def test_listener_failure_does_not_stop_unlinked_ptt(self):
        lid=await self.listen()
        await self.manager.start(self.connection,{"id":2,"config_entry_id":"one","station_id":"gate_main","generation":7})
        capture_id=self.connection.results[-1][1]["capture_id"]
        self.client.reads=[AudioStreamError("audio_identity_mismatch")]
        await self.read(lid); self.assertEqual([],self.client.ends)
        await self.manager.stop(self.connection,{"id":3,"config_entry_id":"one","capture_id":capture_id})
        self.assertEqual([(RUNTIME,7)],self.client.ends)

    async def test_continuous_retry_deadline_closes_linked_mic(self):
        lid=await self.listen(); await self.mic(lid)
        hub=self.manager._listeners[lid].hub
        now=[0.0]; hub._clock=lambda: now[0]
        self.client.reads=[AudioReadReply("retry")]
        await self.read(lid); self.assertEqual({"state":"empty"},self.connection.results[-1][1])
        now[0]=5.0; await self.read(lid)
        self.assertEqual("audio_connection_lost",self.connection.errors[-1][1])
        self.assertEqual([(RUNTIME,7)],self.client.ends)
        self.assertEqual({},self.connection.subscriptions)

    async def test_mic_start_cancelled_during_refresh_removes_pending_owner(self):
        lid=await self.listen(); self.client.refresh_wait=asyncio.Event()
        task=asyncio.create_task(self.mic(lid)); await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.client.refresh_wait=None
        await self.mic(lid)
        self.assertFalse(self.connection.errors); self.assertEqual([(RUNTIME,7)],self.client.opens)

    async def test_listen_start_cancelled_during_refresh_removes_pending_cleanup(self):
        self.client.refresh_wait=asyncio.Event()
        task=asyncio.create_task(self.manager.listen_start(self.connection,
            {"id":1,"config_entry_id":"one","station_id":"gate_main"}))
        await asyncio.sleep(0); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual({},self.connection.subscriptions)
        self.assertFalse(self.manager._pending_cleanups)

    async def test_invalid_entry_is_bounded_without_start_reservation(self):
        for entry_id in (None,[],{}):
            await self.manager.start(self.connection,{"id":1,"config_entry_id":entry_id})
            self.assertEqual("invalid_request",self.connection.errors[-1][1])
        self.assertFalse(self.manager._starting)

    async def test_reconcile_revokes_first_pending_refresh_without_hub(self):
        self.client.refresh_wait=asyncio.Event()
        task=asyncio.create_task(self.manager.listen_start(self.connection,
            {"id":1,"config_entry_id":"one","station_id":"gate_main"}))
        await asyncio.sleep(0)
        await self.manager.reconcile("one",status(runtime="fedcba9876543210",generation=8))
        try:
            self.assertEqual({},self.connection.subscriptions)
            self.assertFalse(self.manager._pending_cleanups)
        finally:
            self.client.refresh_wait.set(); await task
        self.assertEqual([],self.connection.results)
        self.assertEqual("not_found",self.connection.errors[-1][1])
        self.assertFalse(self.manager._listeners)

    async def test_reconcile_revokes_pending_callbacks_before_blocked_mic_end(self):
        lid=await self.listen(); await self.mic(lid)
        self.client.refresh_wait=asyncio.Event(); self.client.end_wait=asyncio.Event()
        pending_connection=SubConnection()
        pending=asyncio.create_task(self.manager.listen_start(pending_connection,
            {"id":4,"config_entry_id":"one","station_id":"gate_main"}))
        await asyncio.sleep(0)
        reconcile=asyncio.create_task(self.manager.reconcile("one",status(generation=8)))
        await asyncio.sleep(0)
        try:
            self.assertEqual({},pending_connection.subscriptions)
            self.assertEqual({},self.connection.subscriptions)
            self.assertFalse(self.manager._pending_cleanups)
            self.assertFalse(self.manager._listeners)
        finally:
            self.client.end_wait.set(); self.client.refresh_wait.set()
            await asyncio.gather(pending,reconcile)
        self.assertEqual([],pending_connection.results)

    async def test_same_identity_reconcile_preserves_pending_refresh(self):
        for existing_hub in (False,True):
            self.connection=SubConnection()
            if existing_hub: await self.listen()
            self.client.refresh_wait=asyncio.Event()
            c=SubConnection()
            task=asyncio.create_task(self.manager.listen_start(c,
                {"id":1,"config_entry_id":"one","station_id":"gate_main"}))
            await asyncio.sleep(0)
            await self.manager.reconcile("one",status())
            self.client.refresh_wait.set(); await task
            self.assertFalse(c.errors); self.assertEqual(7,c.results[-1][1]["generation"])
            self.client.refresh_wait=None
            await self.manager.release_entry("one")

    async def test_real_refresh_out_of_order_never_replaces_new_hub_with_stale(self):
        entered_a=asyncio.Event(); entered_b=asyncio.Event()
        resume_a=asyncio.Event(); resume_b=asyncio.Event()
        new_status=status(runtime="fedcba9876543210",generation=8)
        client=GatedRefreshClient([(status(),entered_a,resume_a),(new_status,entered_b,resume_b)])
        self.hass.data["doorfast"]["one"]=client
        a=SubConnection(); b=SubConnection()
        msg={"id":1,"config_entry_id":"one","station_id":"gate_main"}
        task_a=asyncio.create_task(self.manager.listen_start(a,msg)); await entered_a.wait()
        task_b=asyncio.create_task(self.manager.listen_start(b,msg)); await entered_b.wait()
        resume_b.set(); await task_b
        listener_b=b.results[-1][1]["listener_id"]
        hub_b=self.manager._listeners[listener_b].hub
        resume_a.set(); await task_a
        self.assertEqual(8,client.status["call"]["generation"])
        self.assertEqual([],a.results)
        self.assertEqual("audio_identity_mismatch",a.errors[-1][1])
        self.assertIs(hub_b,self.manager._hubs["one"])
        self.assertFalse(hub_b.closed); self.assertIn(listener_b,self.manager._listeners)
        self.assertEqual({},a.subscriptions)

    async def test_revoked_admin_cannot_read_or_stop_but_internal_cleanup_works(self):
        lid=await self.listen(); await self.mic(lid)
        self.connection.user.is_admin=False
        for method in (self.manager.listen_read,self.manager.listen_stop):
            before=len(self.connection.results)
            await method(self.connection,{"id":5,"config_entry_id":"one","listener_id":lid})
            self.assertTrue(self.connection.errors,"Revoked administrator must receive unauthorized")
            self.assertEqual("unauthorized",self.connection.errors[-1][1])
            self.assertEqual(before,len(self.connection.results))
        self.assertEqual([],self.client.ends)
        await self.connection.disconnect()
        self.assertEqual([(RUNTIME,7)],self.client.ends)
        self.assertEqual({},self.connection.subscriptions)

    async def test_unknown_cached_identity_reconcile_revokes_pending_refresh(self):
        entered=asyncio.Event(); resume=asyncio.Event()
        client=GatedRefreshClient([(status(),entered,resume)]); client.status={}
        self.hass.data["doorfast"]["one"]=client
        task=asyncio.create_task(self.manager.listen_start(self.connection,
            {"id":1,"config_entry_id":"one","station_id":"gate_main"}))
        await entered.wait(); await self.manager.reconcile("one",status())
        try:
            self.assertEqual({},self.connection.subscriptions)
            self.assertFalse(self.manager._pending_cleanups)
        finally:
            resume.set(); await task
        self.assertEqual([],self.connection.results)
        self.assertEqual("not_found",self.connection.errors[-1][1])

    async def test_revoked_admin_stop_denied_without_stopping_linked_mic(self):
        lid=await self.listen(); await self.mic(lid)
        self.connection.user.is_admin=False
        await self.manager.listen_stop(self.connection,
            {"id":5,"config_entry_id":"one","listener_id":lid})
        self.assertTrue(self.connection.errors,"Revoked administrator must receive unauthorized")
        self.assertEqual("unauthorized",self.connection.errors[-1][1])
        self.assertEqual([],self.client.ends)
        self.timers[-1].fire(); await asyncio.sleep(0); await asyncio.sleep(0)
        self.assertEqual([(RUNTIME,7)],self.client.ends)
        self.assertEqual({},self.connection.subscriptions)

    async def test_pending_current_call_survives_reconcile_of_previous_hub(self):
        old=await self.listen(); old_hub=self.manager._listeners[old].hub
        self.client.status=status(generation=8); self.client.refresh_wait=asyncio.Event()
        c=SubConnection()
        task=asyncio.create_task(self.manager.listen_start(c,
            {"id":1,"config_entry_id":"one","station_id":"gate_main"}))
        await asyncio.sleep(0); await self.manager.reconcile("one",status(generation=8))
        self.client.refresh_wait.set(); await task
        self.assertFalse(c.errors); self.assertEqual(8,c.results[-1][1]["generation"])
        self.assertTrue(old_hub.closed); self.assertNotIn(old,self.manager._listeners)

    async def test_legacy_pending_open_revoked_by_changed_call(self):
        for extra in ({},{"station_id":"gate_main"}):
            await self.listen()
            c=SubConnection(); gate=asyncio.Event()
            self.client.open_wait=gate; self.client.open_entered.clear()
            task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one",**extra}))
            await self.client.open_entered.wait()
            await self.manager.reconcile("one",status(generation=8))
            try:
                self.assertEqual({},c.subscriptions)
                self.assertFalse(self.manager._starting)
                self.assertFalse(self.manager._pending_cleanups)
            finally:
                gate.set(); await task
            self.assertEqual([],c.results); self.assertEqual("not_found",c.errors[-1][1])
            self.assertFalse(self.manager._captures)
            self.connection=SubConnection()
        self.assertEqual([(RUNTIME,7),(RUNTIME,7)],self.client.ends)

    async def test_legacy_current_pending_open_survives_old_hub_reconcile(self):
        old=await self.listen(); old_hub=self.manager._listeners[old].hub
        self.client.status=status(generation=8); self.client.open_wait=asyncio.Event()
        c=SubConnection()
        task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one"}))
        await self.client.open_entered.wait()
        await self.manager.reconcile("one",status(generation=8))
        self.client.open_wait.set(); await task
        self.assertFalse(c.errors); self.assertEqual("active",c.results[-1][1]["state"])
        self.assertEqual(8,self.manager._by_entry["one"].producer.generation)
        self.assertTrue(old_hub.closed); self.assertEqual([],self.client.ends)

    async def test_legacy_same_call_pending_open_survives_without_stream_capability(self):
        self.client.status["audio"]={}; self.client.open_wait=asyncio.Event()
        c=SubConnection()
        task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one"}))
        await self.client.open_entered.wait()
        current=status(); current["audio"]={}
        await self.manager.reconcile("one",current)
        self.client.open_wait.set(); await task
        self.assertFalse(c.errors); self.assertEqual("active",c.results[-1][1]["state"])
        self.assertEqual([],self.client.ends)

    async def test_legacy_pending_refresh_unknown_identity_revoked(self):
        self.client.refresh_wait=asyncio.Event(); c=SubConnection()
        task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one"}))
        await asyncio.sleep(0); await self.manager.reconcile("one",{})
        try:
            self.assertEqual({},c.subscriptions)
            self.assertFalse(self.manager._starting)
        finally:
            self.client.refresh_wait.set(); await task
        self.assertEqual([],c.results); self.assertEqual("not_found",c.errors[-1][1])
        self.assertEqual([(RUNTIME,7)],self.client.ends)

    async def test_legacy_pending_open_revoked_by_runtime_station_or_terminal(self):
        changed=[status(runtime="fedcba9876543210"),status(),status()]
        changed[1]["call"]["station_id"]="other_gate"
        changed[2]["call"]["session"]="idle"
        for current in changed:
            c=SubConnection(); self.client.open_wait=asyncio.Event(); self.client.open_entered.clear()
            task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one"}))
            await self.client.open_entered.wait(); await self.manager.reconcile("one",current)
            try:
                self.assertEqual({},c.subscriptions)
                self.assertFalse(self.manager._starting)
            finally:
                self.client.open_wait.set(); await task
            self.assertEqual([],c.results); self.assertEqual("not_found",c.errors[-1][1])
        self.assertEqual([(RUNTIME,7)]*3,self.client.ends)

    async def test_old_legacy_open_cleanup_cannot_clear_new_reservation(self):
        c_old=SubConnection(); old_gate=asyncio.Event(); self.client.open_wait=old_gate
        old=asyncio.create_task(self.manager.start(c_old,{"id":1,"config_entry_id":"one"}))
        await self.client.open_entered.wait(); await self.manager.reconcile("one",status(generation=8))
        self.client.status=status(generation=8)
        c_new=SubConnection(); new_gate=asyncio.Event(); self.client.open_wait=new_gate
        self.client.open_entered.clear()
        new=asyncio.create_task(self.manager.start(c_new,{"id":2,"config_entry_id":"one"}))
        await asyncio.sleep(0)
        try:
            self.assertFalse(c_new.errors)
            self.assertTrue(self.client.open_entered.is_set())
            old_gate.set(); await old
            c_third=SubConnection()
            await self.manager.start(c_third,{"id":3,"config_entry_id":"one"})
            self.assertEqual("producer_busy",c_third.errors[-1][1])
        finally:
            old_gate.set(); new_gate.set(); await asyncio.gather(old,new)
        self.assertEqual([],c_old.results); self.assertEqual([(RUNTIME,7)],self.client.ends)
        self.assertFalse(c_new.errors); self.assertEqual(8,self.manager._by_entry["one"].producer.generation)

    async def test_legacy_pending_uses_producer_fresh_binding_not_initial_cache(self):
        async def refreshed(): return status(generation=8)
        self.client.refresh=refreshed; self.client.open_wait=asyncio.Event()
        c=SubConnection()
        task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one"}))
        await self.client.open_entered.wait()
        self.assertEqual(7,self.client.status["call"]["generation"])
        await self.manager.reconcile("one",status(generation=8))
        self.client.open_wait.set(); await task
        self.assertFalse(c.errors); self.assertEqual(8,self.manager._by_entry["one"].producer.generation)
        self.assertEqual([],self.client.ends)

    async def test_legacy_factory_without_public_binding_is_revoked_conservatively(self):
        class OldFactory:
            def __init__(self, client): self.producer=ws.PcmProducer(client)
            async def start(self): await self.producer.start()
            async def stop(self): await self.producer.stop()
            @property
            def generation(self): return self.producer.generation
            @property
            def sequence(self): return self.producer.sequence
            @property
            def state(self): return self.producer.state
        self.manager.producer_factory=OldFactory
        c=SubConnection(); self.client.open_wait=asyncio.Event()
        task=asyncio.create_task(self.manager.start(c,{"id":2,"config_entry_id":"one"}))
        await self.client.open_entered.wait(); await self.manager.reconcile("one",status())
        try:
            self.assertEqual({},c.subscriptions)
            self.assertFalse(self.manager._starting)
        finally:
            self.client.open_wait.set(); await task
        self.assertEqual([],c.results); self.assertEqual("not_found",c.errors[-1][1])
        self.assertEqual([(RUNTIME,7)],self.client.ends)
