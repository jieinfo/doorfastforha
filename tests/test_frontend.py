import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import subprocess
import unittest

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "doorfast"


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def async_register_static_paths(self, paths):
        self.calls.append(paths)


class FakeHass:
    def __init__(self):
        self.data = {}
        self.http = FakeHttp()


class FrontendRegistrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        http = types.ModuleType("homeassistant.components.http")
        http.StaticPathConfig = lambda url, path, cache: (url, path, cache)
        frontend = types.ModuleType("homeassistant.components.frontend")
        frontend.add_extra_js_url = lambda hass, url: hass.data.setdefault("extra_modules", set()).add(url)
        frontend.remove_extra_js_url = lambda hass, url: hass.data.setdefault("extra_modules", set()).discard(url)
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")
        sys.modules["homeassistant.components"] = types.ModuleType("homeassistant.components")
        sys.modules["homeassistant.components.http"] = http
        sys.modules["homeassistant.components.frontend"] = frontend
        package = types.ModuleType("custom_components.doorfast")
        package.__path__ = [str(COMPONENT)]
        sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
        sys.modules["custom_components.doorfast"] = package
        const_spec = importlib.util.spec_from_file_location("custom_components.doorfast.const", COMPONENT / "const.py")
        const_module = importlib.util.module_from_spec(const_spec)
        sys.modules["custom_components.doorfast.const"] = const_module
        const_spec.loader.exec_module(const_module)
        spec = importlib.util.spec_from_file_location("custom_components.doorfast.frontend", COMPONENT / "frontend.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    async def test_registers_static_assets_and_module_once(self):
        hass = FakeHass()
        await asyncio.gather(
            self.module.async_register_frontend(hass, "one"),
            self.module.async_register_frontend(hass, "one"),
        )
        self.assertEqual(1, len(hass.http.calls))
        [static_path] = hass.http.calls[0]
        self.assertEqual("/doorfast_static", static_path[0])
        self.assertTrue(static_path[1].endswith("custom_components/doorfast/frontend"))
        self.assertTrue(static_path[2])
        module_url = "/doorfast_static/doorfast-ptt-card.mjs?v=20261012.1"
        self.assertEqual({module_url}, hass.data["extra_modules"])

        await self.module.async_register_frontend(hass, "two")
        await self.module.async_unregister_frontend(hass, "one")
        self.assertEqual({module_url}, hass.data["extra_modules"])
        await self.module.async_unregister_frontend(hass, "two")
        self.assertEqual(set(), hass.data["extra_modules"])

        # The HTTP route remains registered and a reload only restores the module URL.
        await self.module.async_register_frontend(hass, "one")
        self.assertEqual(1, len(hass.http.calls))
        self.assertEqual({module_url}, hass.data["extra_modules"])

    async def test_registered_module_imports_and_requested_worklet_use_same_cache_version(self):
        hass = FakeHass()
        await self.module.async_register_frontend(hass, "one")
        [card_url] = hass.data["extra_modules"]
        [route] = hass.http.calls[0]
        # Load the served module graph and exercise actual worklet.addModule,
        # rather than asserting that source text contains a version string.
        script = r'''
const vm = require("node:vm");
const fs = require("node:fs/promises");
const assert = require("node:assert/strict");
const [root, route, directory] = JSON.parse(process.argv[1]);
const modules = new Map(); const loaded = []; const worklets = [];
const target = { addEventListener() {}, setAttribute() {}, textContent: "" };
const context = vm.createContext({
  console, setTimeout, clearTimeout, ArrayBuffer, Uint8Array, Float32Array, DataView,
  HTMLElement: class { attachShadow() { this.shadowRoot = { innerHTML: "", querySelector: () => target }; } },
  customElements: { get() {}, define() {} },
  window: { customCards: [], isSecureContext: true }, document: {},
  navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {}, addEventListener() {} }] }) } },
  AudioWorkletNode: class { constructor() { this.port = {}; } disconnect() {} },
  AudioWorkletProcessor: class {}, sampleRate: 48000, registerProcessor() {},
});
async function load(url) {
  if (modules.has(url)) return modules.get(url);
  const parsed = new URL(url, "http://test");
  assert.equal(parsed.search, "?v=20261012.1", url);
  assert.ok(parsed.pathname.startsWith(route + "/"));
  const source = await fs.readFile(directory + parsed.pathname.slice(route.length), "utf8");
  loaded.push(url);
  const mod = new vm.SourceTextModule(source, { context, identifier: url });
  modules.set(url, mod);
  await mod.link((specifier, importer) => load(new URL(specifier, "http://test" + importer.identifier).pathname + new URL(specifier, "http://test" + importer.identifier).search));
  return mod;
}
context.AudioContext = class {
  constructor() { this.state = "running"; this.audioWorklet = { addModule: async (url) => { worklets.push(url); const mod = await load(url); await mod.evaluate(); } }; }
  async resume() {} async close() { this.state = "closed"; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
};
(async () => {
  const mod = await load(root); await mod.evaluate();
  const card = new mod.namespace.DoorfastPttCard();
  card.setConfig({ config_entry_id: "one" });
  card.hass = { callWS: async () => ({ capture_id: "capture" }) };
  await card._start(); assert.equal(card._status, "active"); await card._stop();
  assert.deepEqual(worklets, ["/doorfast_static/doorfast-pcm-worklet.mjs?v=20261012.1"]);
  for (const name of ["audio-stream.mjs", "audio-messages.mjs", "pcm-playback.mjs", "pcm-dsp.mjs"]) {
    assert.ok(loaded.includes(route + "/" + name + "?v=20261012.1"), name);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(
            ["node", "--no-warnings", "--experimental-vm-modules", "-e", script,
             json.dumps([card_url, route[0], route[1]])], capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
