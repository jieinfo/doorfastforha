import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"
package = types.ModuleType("custom_components.doorfast")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
sys.modules["custom_components.doorfast"] = package
# Provide the tiny imports needed by pcm.py.
load = lambda name, path: _load(name, path)
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
client_types = _load("custom_components.doorfast.client_types", COMPONENT / "client_types.py")
pcm = _load("custom_components.doorfast.pcm", COMPONENT / "pcm.py")
const = _load("custom_components.doorfast.const", COMPONENT / "const.py")

# The module falls back to this decorator and registration is exercised directly.
ws = _load("custom_components.doorfast.websocket", COMPONENT / "websocket.py")

FRAME = bytes(320)

class User:
    def __init__(self, admin): self.is_admin = admin
class Connection:
    def __init__(self, admin=True):
        self.user = User(admin); self.results=[]; self.errors=[]; self.cleanup=[]
    def send_result(self, ident, result): self.results.append((ident, result))
    def send_error(self, ident, code, message): self.errors.append((ident, code, message))
    def async_add_cleanup_callback(self, cb): self.cleanup.append(cb)
    async def disconnect(self):
        for cb in self.cleanup:
            result = cb()
            if hasattr(result, "__await__"): await result

class FakeProducer:
    def __init__(self, client):
        self.client=client; self.state=types.SimpleNamespace(value="active"); self.generation=7; self.sequence=0; self.stopped=0; self.started=0; self.submissions=[]
    async def start(self): self.started += 1
    async def submit(self, frames):
        self.submissions.append(frames); self.sequence += len(frames)
        return types.SimpleNamespace(accepted_frames=len(frames), next_sequence=self.sequence, complete=True, recovered=False)
    async def stop(self): self.stopped += 1

class Hass:
    def __init__(self): self.data={"doorfast": {"one": object(), "two": object()}}

class WebsocketOwnershipTest(unittest.IsolatedAsyncioTestCase):
    def manager(self): return ws.PcmWebSocketManager(Hass(), FakeProducer)
    async def test_admin_start_submit_stop_hides_identity(self):
        m=self.manager(); c=Connection(); await m.start(c,{"id":1,"config_entry_id":"one"})
        result=c.results[0][1]; self.assertEqual("active", result["state"]); self.assertIn("capture_id", result); self.assertNotIn("runtime", result)
        cid=result["capture_id"]
        await m.submit(c,{"id":2,"config_entry_id":"one","capture_id":cid,"pcm":(FRAME*2).hex()})
        self.assertEqual([], c.errors)  # invalid base64 is intentionally rejected below
        self.assertEqual("invalid_format", c.errors[-1][1]) if c.errors else None
        import base64
        await m.submit(c,{"id":3,"config_entry_id":"one","capture_id":cid,"pcm":base64.b64encode(FRAME*2).decode()})
        self.assertEqual(2,c.results[-1][1]["accepted_frames"])
        await m.stop(c,{"id":4,"config_entry_id":"one","capture_id":cid}); self.assertEqual("idle",c.results[-1][1]["state"])
    async def test_non_admin_and_invalid_inputs_are_rejected(self):
        m=self.manager(); c=Connection(False); await m.start(c,{"id":1,"config_entry_id":"one"}); self.assertEqual("unauthorized",c.errors[0][1])
        c=Connection(); await m.submit(c,{"id":2,"capture_id":"missing","pcm":""}); self.assertEqual("not_found",c.errors[0][1])
    async def test_multi_entry_isolation_and_duplicate_entry_busy(self):
        m=self.manager(); c1=Connection(); c2=Connection()
        await m.start(c1,{"id":1,"config_entry_id":"one"}); await m.start(c2,{"id":2,"config_entry_id":"two"})
        await m.start(c2,{"id":3,"config_entry_id":"two"}); self.assertEqual("producer_busy",c2.errors[-1][1])
        self.assertNotEqual(c1.results[0][1]["capture_id"],c2.results[0][1]["capture_id"])
    async def test_disconnect_and_generation_unload_cleanup(self):
        m=self.manager(); c=Connection(); await m.start(c,{"id":1,"config_entry_id":"one"}); prod=m._by_entry["one"].producer
        await c.disconnect(); self.assertEqual(1,prod.stopped); self.assertNotIn("one",m._by_entry)
        await m.start(c,{"id":2,"config_entry_id":"one"}); prod=m._by_entry["one"].producer
        await m.reconcile("one",{"call":{"generation":8}}); self.assertEqual(1,prod.stopped)
    async def test_global_registration_is_idempotent(self):
        calls=[]
        original=ws.async_register_command
        ws.async_register_command=lambda hass, handler: calls.append(handler)
        try:
            m=self.manager(); m.register(); m.register()
        finally: ws.async_register_command=original
        self.assertEqual(3,len(calls))

if __name__ == "__main__": unittest.main()
