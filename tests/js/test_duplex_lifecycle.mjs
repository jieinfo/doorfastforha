import assert from "node:assert/strict";
import test from "node:test";

// Only DOM, Web Audio, clock, microphone and HA transport are external doubles.
class Target {
  constructor() { this.handlers = new Map(); this.attributes = {}; }
  addEventListener(type, callback) {
    const handlers = this.handlers.get(type) ?? new Set();
    handlers.add(callback); this.handlers.set(type, handlers);
  }
  removeEventListener(type, callback) { this.handlers.get(type)?.delete(callback); }
  setAttribute(name, value) { this.attributes[name] = value; }
  dispatch(type) { for (const handler of this.handlers.get(type) ?? []) handler({ preventDefault() {} }); }
}
globalThis.HTMLElement = class {
  attachShadow() {
    let button, label;
    this.shadowRoot = {
      set innerHTML(value) { button = new Target(); label = { textContent: "" }; },
      querySelector(selector) { return selector === "button" ? button : label; },
    };
    this.shadowRoot.innerHTML = "";
  }
};
globalThis.customElements = { values: new Map(), get(name) { return this.values.get(name); }, define(name, value) { this.values.set(name, value); } };
globalThis.window = new Target(); window.customCards = [];
globalThis.document = new Target(); document.hidden = false;
const { DoorfastPttCard } = await import("../../custom_components/doorfast/frontend/doorfast-ptt-card.mjs");
const { PcmAudioPlayback, PcmBatchSender } = await import("../../custom_components/doorfast/frontend/audio-stream.mjs?v=20261012.1");
const { PcmAudioOutput } = await import("../../custom_components/doorfast/frontend/pcm-playback.mjs?v=20261012.1");

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let index = 0; index < 30; index++) await Promise.resolve(); };

function wireChunk(listenerId, revision = 40) {
  // 100 ms, mono 8 kHz s16le, with independently known endpoint amplitudes.
  const pcm = Buffer.alloc(1600);
  pcm.writeInt16LE(-32768, 0); pcm.writeInt16LE(32767, 1598);
  return { state: "chunk", listener_id: listenerId, generation: 7,
    previous_revision: revision - 10, revision, discontinuity: false, pcm: pcm.toString("base64") };
}

function setup(t, { mode = "duplex", secure = true, permission } = {}) {
  const calls = [], contexts = [], timers = new Map(), worklets = [], reads = [];
  const submitGate = deferred();
  let timerId = 0, listenerCount = 0, captureCount = 0, readInflight = 0, maxReadInflight = 0, micCalls = 0;
  const originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  globalThis.setTimeout = (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  t.after(() => { globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear; });
  const track = new Target(); track.stopCount = 0; track.stop = () => { track.stopCount++; };
  const stream = { getTracks: () => [track] };
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia: async () => { micCalls++; return permission ? await permission.promise : stream; },
  } } });
  window.isSecureContext = secure;
  globalThis.AudioContext = class {
    constructor() {
      this.currentTime = 0; this.state = "suspended"; this.destination = {};
      this.resumeCount = 0; this.closeCount = 0; this.sources = []; this.captureSources = [];
      this.audioWorklet = { addModule: async () => {} }; contexts.push(this);
    }
    async resume() { this.resumeCount++; this.state = "running"; }
    async close() { this.closeCount++; this.state = "closed"; }
    createBuffer(channels, length, rate) {
      const data = new Float32Array(length);
      return { channels, length, rate, getChannelData: () => data };
    }
    createBufferSource() {
      const source = {
        stopCount: 0, disconnectCount: 0,
        connect(destination) { this.destination = destination; },
        start(time) { this.startedAt = time; },
        stop() { this.stopCount++; },
        disconnect() { this.disconnectCount++; },
      };
      this.sources.push(source); return source;
    }
    createMediaStreamSource(value) {
      assert.equal(value, stream);
      const source = { disconnectCount: 0, connect(node) { this.node = node; }, disconnect() { this.disconnectCount++; } };
      this.captureSources.push(source); return source;
    }
  };
  globalThis.AudioWorkletNode = class {
    constructor() { this.port = {}; this.disconnectCount = 0; worklets.push(this); }
    disconnect() { this.disconnectCount++; }
  };
  const connection = new Target();
  const card = new DoorfastPttCard();
  card.setConfig({ config_entry_id: "one", station_id: "gate_main", mode, name: "Main gate intercom" });
  card.hass = { connection, locale: { language: "en" }, callWS: async (message) => {
    calls.push(message);
    switch (message.type) {
      case "doorfast/audio/listen_start":
        return { listener_id: `listener-${++listenerCount}`, station_id: "gate_main", generation: 7, sample_rate: 8000, channels: 1, sample_format: "s16le" };
      case "doorfast/audio/start": return { capture_id: `capture-${++captureCount}`, state: "active", sequence: 0 };
      case "doorfast/audio/listen_read":
        readInflight++; maxReadInflight = Math.max(maxReadInflight, readInflight);
        try { return reads.length ? await reads.shift() : { state: "empty" }; }
        finally { readInflight--; }
      case "doorfast/audio/submit": return await submitGate.promise;
      case "doorfast/audio/stop": case "doorfast/audio/listen_stop": return { state: "idle" };
      default: throw new Error("Unexpected WS command");
    }
  } };
  card.connectedCallback();
  t.after(async () => { card.disconnectedCallback(); await flush(); });
  async function tick() {
    const pending = [...timers.values()]; timers.clear();
    for (const { callback } of pending) callback();
    await flush();
  }
  return { card, calls, contexts, worklets, reads, timers, track, stream, connection, submitGate, tick,
    get micCalls() { return micCalls; }, get maxReadInflight() { return maxReadInflight; } };
}

function assertAudioNode(context, index = 0) {
  const node = context.sources[index];
  assert.ok(node, "the card must produce a real scheduled PCM source");
  assert.equal(node.buffer.channels, 1); assert.equal(node.buffer.rate, 8000); assert.equal(node.buffer.length, 800);
  assert.equal(node.buffer.getChannelData(0)[0], -1);
  assert.equal(node.buffer.getChannelData(0)[799], 32767 / 32768);
  assert.equal(node.startedAt, 0.12);
  assert.equal(node.destination, context.destination);
  return node;
}

test("real duplex output and bounded sender release on stop during a pending submit", async (t) => {
  // A broken decode/output or missing stop epoch would leave PCM nodes or resend frames.
  const env = setup(t); const { card } = env;
  env.reads.push(wireChunk("listener-1"));
  card.shadowRoot.querySelector("button").dispatch("click"); await flush();
  assert.equal(card._status, "active");
  const session = card._session;
  assert.ok(session.playback instanceof PcmAudioPlayback);
  assert.ok(session.output instanceof PcmAudioOutput);
  assert.ok(session.sender instanceof PcmBatchSender);
  await env.tick();
  const context = env.contexts[0], audioNode = assertAudioNode(context);
  assert.equal(context.resumeCount, 1);
  assert.deepEqual(env.calls[0], { type: "doorfast/audio/listen_start", config_entry_id: "one", station_id: "gate_main" });
  assert.equal(env.calls.find((message) => message.type === "doorfast/audio/start").listener_id, "listener-1");
  for (let index = 0; index < 7; index++) env.worklets[0].port.onmessage({ data: { type: "frame", frame: new ArrayBuffer(320) } });
  assert.equal(session.sender.queuedFrames, 5);
  assert.equal(session.sender.droppedFrames, 1);
  const submit = env.calls.filter((message) => message.type === "doorfast/audio/submit");
  assert.equal(submit.length, 1); assert.equal(Buffer.from(submit[0].pcm, "base64").length, 320);
  assert.equal(submit[0].capture_id, "capture-1");
  card.shadowRoot.querySelector("button").dispatch("click"); await flush();
  env.submitGate.resolve({ accepted_frames: 1, next_sequence: 1 }); await flush();
  assert.equal(env.calls.filter((message) => message.type === "doorfast/audio/submit").length, 1);
  assert.equal(card._status, "idle"); assert.equal(card._session, null);
  assert.equal(audioNode.stopCount, 1); assert.equal(audioNode.disconnectCount, 1); assert.equal(audioNode.onended, null);
  assert.equal(context.closeCount, 1); assert.equal(env.track.stopCount, 1);
  assert.equal(context.captureSources[0].disconnectCount, 1);
  assert.equal(env.worklets[0].disconnectCount, 1); assert.equal(env.worklets[0].port.onmessage, null);
  assert.equal(session.output.sources.size, 0); assert.equal(env.timers.size, 0);
  assert.deepEqual(env.calls.filter((message) => ["doorfast/audio/stop", "doorfast/audio/listen_stop"].includes(message.type)), [
    { type: "doorfast/audio/stop", config_entry_id: "one", capture_id: "capture-1" },
    { type: "doorfast/audio/listen_stop", config_entry_id: "one", listener_id: "listener-1" },
  ]);
});

test("stop and restart with generation seven discard late PCM and serialize real card reads", async (t) => {
  // A missing card read gate or epoch check would overlap reads or play the old response.
  const env = setup(t, { mode: "listen" }), oldRead = deferred();
  env.reads.push(oldRead.promise, wireChunk("listener-2"));
  await env.card._toggle(); const oldOutput = env.card._session.output; await env.tick();
  await env.card._toggle(); await env.card._toggle(); await env.tick();
  assert.equal(env.calls.filter((message) => message.type === "doorfast/audio/listen_read").length, 1);
  oldRead.resolve(wireChunk("listener-1")); await flush();
  assert.equal(env.contexts[0].sources.length, 0);
  assert.equal(oldOutput.sources.size, 0);
  assertAudioNode(env.contexts[1]);
  assert.equal(env.maxReadInflight, 1);
  assert.deepEqual(env.calls.filter((message) => message.type === "doorfast/audio/listen_read").map((message) => message.listener_id), ["listener-1", "listener-2"]);
  assert.equal(env.micCalls, 0);
  await env.card._stop();
  assert.equal(env.contexts[0].closeCount, 1); assert.equal(env.contexts[1].closeCount, 1);
  assert.equal(env.contexts[1].sources[0].stopCount, 1);
  assert.equal(env.timers.size, 0); assert.equal(env.card._readFlight, null);
});

for (const condition of ["HTTP", "denied"]) {
  test(`${condition} duplex uses real listen output and requires a new click after disconnect`, async (t) => {
    // Permission fallback must still play PCM; a disconnected session must never reacquire mic.
    const permission = condition === "denied" ? deferred() : undefined;
    const env = setup(t, { secure: condition !== "HTTP", permission });
    env.reads.push(wireChunk("listener-1"));
    const starting = env.card._toggle();
    if (permission) permission.reject(Object.assign(new Error("synthetic denial"), { name: "NotAllowedError" }));
    await starting; await env.tick();
    assert.equal(env.card._status, "listening"); assertAudioNode(env.contexts[0]);
    assert.equal(env.micCalls, condition === "HTTP" ? 0 : 1);
    assert.equal(env.calls.some((message) => message.type === "doorfast/audio/start"), false);
    env.connection.dispatch("disconnected"); await flush(); await env.tick();
    assert.equal(env.card._status, "idle"); assert.equal(env.contexts.length, 1);
    assert.equal(env.contexts[0].sources[0].stopCount, 1); assert.equal(env.contexts[0].closeCount, 1);
    assert.equal(env.timers.size, 0);
    assert.equal(env.calls.filter((message) => message.type === "doorfast/audio/listen_start").length, 1);
  });
}
