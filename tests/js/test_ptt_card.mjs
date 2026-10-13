import assert from "node:assert/strict";
import test from "node:test";

globalThis.HTMLElement = class {
  attachShadow() {
    let button, label, html;
    this.shadowRoot = {
      get innerHTML() { return html; },
      set innerHTML(value) { html = value; button = new Target(); label = { textContent: "" }; },
      querySelector: (selector) => selector === "button" ? button : label,
    };
    this.shadowRoot.innerHTML = "";
  }
};
class Target {
  constructor() { this.handlers = {}; this.attributes = {}; }
  addEventListener(type, callback) { (this.handlers[type] ??= []).push(callback); }
  removeEventListener(type, callback) { this.handlers[type] = (this.handlers[type] ?? []).filter((item) => item !== callback); }
  setAttribute(key, value) { this.attributes[key] = value; }
  setPointerCapture() {}
  dispatch(type, detail = {}) { for (const callback of this.handlers[type] ?? []) callback({ preventDefault() {}, ...detail }); }
}
globalThis.customElements = { values: new Map(), get(name) { return this.values.get(name); }, define(name, value) { this.values.set(name, value); } };
globalThis.window = { customCards: [], addEventListener() {}, removeEventListener() {} };
globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };

const { DoorfastPttCard } = await import("../../custom_components/doorfast/frontend/doorfast-ptt-card.mjs");

test("status changes update the existing press target without replacing it", () => {
  const card = new DoorfastPttCard();
  const attributes = {};
  const button = { setAttribute: (name, value) => { attributes[name] = value; }, textContent: "" };
  const label = { textContent: "" };
  card.shadowRoot.querySelector = (selector) => selector === "button" ? button : label;
  card._render = () => { throw new Error("status update replaced the pointer target"); };
  card._setStatus("starting");
  assert.equal(attributes["aria-pressed"], "true");
  assert.equal(button.textContent, "Release to stop");
  assert.equal(label.textContent, "starting");
});

test("configuration changes stop a real PTT capture with its stored entry identity", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup("ptt");
  await env.card._start();
  env.card.setConfig({ config_entry_id: "new" }); await flush();
  assert.deepEqual(env.calls.filter((message) => message.type.endsWith("/stop")), [{ type: "doorfast/audio/stop", config_entry_id: "one", capture_id: "capture-1" }]);
  assert.equal(env.contexts[0].closeCount, 1);
});

test("audio playback is an explicit lifecycle hook", async () => {
  const card = new DoorfastPttCard();
  let stopped = 0;
  card.setAudioPlayback({ start: () => {}, stop: () => { stopped += 1; } });
  card.setAudioPlayback(null);
  assert.equal(stopped, 1);
});

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
function setup(mode = "duplex", secure = true) {
  const calls = []; const contexts = []; const tracks = []; const reads = []; const workletUrls = [];
  let micCalls = 0; let listenerCount = 0; let captureCount = 0;
  const track = new Target(); track.stopped = 0; track.stop = () => { track.stopped++; }; tracks.push(track);
  const stream = { getTracks: () => [track] };
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: async () => { micCalls++; return stream; } } } });
  window.isSecureContext = secure;
  globalThis.AudioContext = class {
    constructor() { this.state = "suspended"; this.currentTime = 0; this.destination = {}; this.closeCount = 0; this.sources = []; this.audioWorklet = { addModule: async (url) => { workletUrls.push(url); } }; contexts.push(this); }
    async resume() { this.state = "running"; }
    async close() { this.closeCount++; this.state = "closed"; }
    createMediaStreamSource() { return { connect() { this.connected = true; }, disconnect() { this.disconnected = true; } }; }
    createBuffer(channels, length) { const data = new Float32Array(length); return { getChannelData: () => data }; }
    createBufferSource() { const source = { connect() {}, start(time) { this.started = time; }, stop() { this.stopped = true; }, disconnect() { this.disconnected = true; } }; this.sources.push(source); return source; }
  };
  globalThis.AudioWorkletNode = class { constructor() { this.port = {}; } disconnect() { this.disconnected = true; } };
  const card = new DoorfastPttCard();
  card.setConfig({ config_entry_id: "one", station_id: "gate_main", mode });
  card.hass = { locale: { language: "en" }, callWS: async (message) => {
    calls.push(message);
    if (message.type.endsWith("listen_start")) return { listener_id: `listener-${++listenerCount}`, station_id: "gate_main", generation: 7, sample_rate: 8000, channels: 1, sample_format: "s16le" };
    if (message.type.endsWith("/start")) return { capture_id: `capture-${++captureCount}`, state: "active", sequence: 0 };
    if (message.type.endsWith("listen_read")) return reads.length ? await reads.shift() : { state: "empty" };
    return { state: "idle" };
  } };
  return { card, calls, contexts, tracks, reads, workletUrls, stream, get micCalls() { return micCalls; } };
}

test("new modes require a station, accept backend-bound generation, and reject unknown modes", () => {
  const card = new DoorfastPttCard();
  assert.throws(() => card.setConfig({ config_entry_id: "one", mode: "duplex" }));
  assert.throws(() => card.setConfig({ config_entry_id: "one", station_id: "gate", mode: "other" }));
  assert.throws(() => card.setConfig({ config_entry_id: "one", station_id: "gate", mode: "listen", generation: 0 }));
  card.setConfig({ config_entry_id: "one" });
  assert.equal(card._config.mode, "ptt");
  card.setConfig({ config_entry_id: "one", station_id: "gate", mode: "listen" });
});

test("duplex click stays active through pointerup and second click releases native resources", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const { card } = env;
  env.reads.push({ state: "chunk", listener_id: "listener-1", generation: 7, previous_revision: 0, revision: 1, discontinuity: false, pcm: "AIA=" });
  const button = card.shadowRoot.querySelector("button");
  button.dispatch("click"); await flush();
  assert.equal(card._status, "active");
  button.dispatch("pointerup"); await flush();
  assert.equal(card._status, "active");
  t.mock.timers.tick(0); await flush();
  assert.equal(env.contexts[0].sources.length, 1);
  assert.equal(env.contexts[0].sources[0].buffer.getChannelData(0)[0], -1);
  assert.deepEqual(env.calls.find((message) => message.type.endsWith("listen_start")), { type: "doorfast/audio/listen_start", config_entry_id: "one", station_id: "gate_main" });
  assert.equal(env.calls.find((message) => message.type.endsWith("/start")).listener_id, "listener-1");
  const source = card._session.source; const worklet = card._session.worklet;
  button.dispatch("click"); await flush();
  assert.equal(card._status, "idle");
  assert.equal(env.contexts[0].closeCount, 1);
  assert.equal(env.contexts[0].sources[0].stopped, true);
  assert.equal(env.contexts[0].sources[0].disconnected, true);
  assert.equal(source.disconnected, true); assert.equal(worklet.disconnected, true);
  assert.equal(worklet.port.onmessage, null);
  assert.equal(env.tracks[0].stopped, 1);
  assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, 1);
});

test("HTTP duplex receives native audio without requesting microphone and visibly stays listen-only", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup("duplex", false);
  await env.card._toggle();
  assert.equal(env.card._status, "listening");
  assert.equal(env.micCalls, 0);
  assert.equal(env.card.shadowRoot.querySelector(".status").textContent, "Listening only: HTTPS required");
  assert.equal(env.calls.some((message) => message.type.endsWith("/start")), false);
  await env.card._toggle();
});

test("denied permission stays listening even if the unused microphone worklet fails", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup();
  navigator.mediaDevices.getUserMedia = async () => { throw Object.assign(new Error("private denial detail"), { name: "NotAllowedError" }); };
  const Original = AudioContext;
  globalThis.AudioContext = class extends Original { constructor() { super(); this.audioWorklet.addModule = async () => { throw new Error("worklet blocked"); }; } };
  await env.card._toggle();
  assert.equal(env.card._status, "listening");
  assert.equal(env.card.shadowRoot.querySelector(".status").textContent, "Listening only: Microphone denied");
  assert.equal(env.contexts[0].closeCount, 0);
  assert.equal(env.calls.some((message) => message.type.endsWith("/start")), false);
  await env.card._stop();
});

test("replacing the HA connection releases the current session and never auto-restarts", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); await env.card._toggle();
  env.card.hass = { connection: new Target(), callWS: async () => { throw new Error("must not auto-start"); } };
  await flush();
  assert.equal(env.card._status, "idle");
  assert.equal(env.contexts[0].closeCount, 1);
  assert.equal(env.tracks[0].stopped, 1);
  assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, 1);
});

test("backend not-ready feedback is localized and never shows raw error body", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const callWS = env.card._hass.callWS;
  env.card._hass.language = "zh-CN"; env.card._hass.locale = undefined;
  env.card._hass.callWS = async (message) => {
    if (message.type.endsWith("/start")) throw { code: "status_not_ready", message: "private server detail" };
    return callWS(message);
  };
  await env.card._toggle();
  assert.equal(env.card.shadowRoot.querySelector(".status").textContent, "error: 通话尚未建立");
  assert.equal(env.contexts[0].closeCount, 1);
  assert.equal(env.tracks[0].stopped, 1);
});

test("continuous mode rejects station IDs the actual bridge cannot bind", () => {
  for (const station_id of ["Gate", "gate/main", " gate", "门口", "a".repeat(33)]) {
    assert.throws(() => new DoorfastPttCard().setConfig({ config_entry_id: "one", mode: "listen", station_id }));
  }
});

test("permission denial is reported while an unused worklet request remains pending", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const pending = deferred();
  navigator.mediaDevices.getUserMedia = async () => { throw Object.assign(new Error("denied"), { name: "NotAllowedError" }); };
  const Original = AudioContext;
  globalThis.AudioContext = class extends Original { constructor() { super(); this.audioWorklet.addModule = () => pending.promise; } };
  const starting = env.card._toggle(); await flush();
  assert.equal(env.card._status, "listening");
  pending.reject(new Error("unused module failed")); await starting;
  await env.card._stop();
});

test("Chinese HTTP feedback uses the specified listen-only message", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup("duplex", false); env.card._hass.locale.language = "zh-CN";
  await env.card._toggle();
  assert.equal(env.card.shadowRoot.querySelector(".status").textContent, "仅收听：麦克风需要HTTPS");
  assert.equal(env.card.shadowRoot.querySelector("button").attributes["aria-label"], "停止对讲");
  await env.card._stop();
});

test("a microphone ending while worklet startup is pending cancels the original session", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const module = deferred(); const Original = AudioContext;
  globalThis.AudioContext = class extends Original { constructor() { super(); this.audioWorklet.addModule = () => module.promise; } };
  const starting = env.card._toggle(); await flush();
  assert.equal(env.card._status, "starting");
  env.tracks[0].dispatch("ended"); await flush();
  assert.equal(env.card._status, "idle");
  assert.equal(env.contexts[0].closeCount, 1);
  module.resolve(); await starting;
  assert.equal(env.calls.some((message) => message.type.endsWith("/start")), false);
  assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, 1);
});

test("a queued disconnection callback from the old connection cannot stop its replacement", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const callWS = env.card._hass.callWS;
  const oldConnection = new Target();
  env.card.hass = { connection: oldConnection, callWS }; await env.card._toggle();
  const queued = oldConnection.handlers.disconnected[0];
  env.card.hass = { connection: new Target(), callWS }; await flush(); await env.card._toggle();
  queued(); await flush();
  assert.equal(env.card._status, "active");
  assert.equal(env.contexts[1].closeCount, 0);
  await env.card._stop();
});

test("a queued disconnection callback is scoped to the original session on the same connection", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const callWS = env.card._hass.callWS; const connection = new Target();
  env.card.hass = { connection, callWS }; await env.card._toggle();
  const queued = connection.handlers.disconnected[0];
  await env.card._stop(); await env.card._toggle();
  queued(); await flush();
  assert.equal(env.card._status, "active");
  assert.equal(env.contexts[1].closeCount, 0);
  await env.card._stop();
});

test("a click before HA is available reports a safe error without requesting microphone", async () => {
  const env = setup(); env.card.hass = undefined;
  await env.card._toggle();
  assert.equal(env.card._status, "error: Audio connection lost");
  assert.equal(env.micCalls, 0);
});

test("synchronous worklet setup failure still owns a pending permission grant", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const permission = deferred(); const Original = AudioContext;
  navigator.mediaDevices.getUserMedia = () => permission.promise;
  globalThis.AudioContext = class extends Original { constructor() { super(); this.audioWorklet.addModule = () => { throw new Error("unsupported worklet"); }; } };
  const starting = env.card._toggle(); await flush();
  assert.equal(env.card._status, "starting", "worklet failure waits for the permission result before releasing capture resources");
  permission.resolve(env.stream); await starting;
  assert.equal(env.tracks[0].stopped, 1);
  assert.equal(env.contexts[0].closeCount, 1);
  assert.equal(env.card._status, "error: Audio connection lost");
});

test("configured generation is sent as an assertion and mismatched listener replies are released", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  for (const generation of [7, 8]) {
    const env = setup(); env.card.setConfig({ config_entry_id: "one", station_id: "gate_main", mode: "duplex", generation });
    await env.card._toggle();
    assert.equal(env.calls.find((message) => message.type.endsWith("listen_start")).generation, generation);
    if (generation === 7) {
      assert.equal(env.card._status, "active");
      const start = env.calls.find((message) => message.type.endsWith("/start"));
      assert.equal(start.generation, 7); assert.equal(start.listener_id, "listener-1");
    } else {
      assert.equal(env.card._status, "error: Audio connection lost");
      assert.equal(env.calls.some((message) => message.type.endsWith("/start")), false);
      assert.equal(env.contexts[0].closeCount, 1);
      assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, 1);
    }
    await env.card._stop();
  }
});

for (const [code, message] of [
  ["producer_busy", "Microphone busy"], ["unsupported_audio_stream", "Update Doorfast"],
  ["audio_identity_mismatch", "Call not established / Station mismatch"], ["audio_listener_limit", "Audio connection lost"],
  ["audio_listener_closed", "Audio connection lost"], ["audio_listener_not_found", "Audio connection lost"],
  ["not_found", "Audio connection lost"], ["private_code", "Audio connection lost"],
]) {
  test(`stable backend code ${code} has safe user feedback`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const env = setup("listen"); env.card._hass.callWS = async () => { throw { code, message: "private details" }; };
    await env.card._toggle();
    assert.equal(env.card.shadowRoot.querySelector(".status").textContent, `error: ${message}`);
    assert.equal(env.contexts[0].closeCount, 1);
    assert.equal(env.micCalls, 0);
  });
}

// These integration checks exercise the real card/controller/output at the
// browser/HA boundaries; removing session scoping would misrelease the next call.
test("cancel while permission is pending closes the late original track without touching its replacement", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const permission = deferred(); let requests = 0;
  const late = new Target(); late.stopped = 0; late.stop = () => { late.stopped++; };
  navigator.mediaDevices.getUserMedia = () => ++requests === 1 ? permission.promise : Promise.resolve(env.stream);
  const original = env.card._toggle(); await flush();
  assert.equal(env.card._status, "starting");
  await env.card._toggle();
  assert.equal(env.card._status, "idle");
  await env.card._toggle();
  assert.equal(env.card._status, "active");
  permission.resolve({ getTracks: () => [late] }); await original;
  assert.equal(late.stopped, 1);
  assert.equal(env.contexts[0].closeCount, 1);
  assert.equal(env.contexts[1].closeCount, 0);
  assert.equal(env.tracks[0].stopped, 0);
  assert.equal(env.card._status, "active");
  await env.card._stop();
});

test("late listener identity releases the original entry without stopping the replacement", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup("listen"); const start = deferred(); const base = env.card._hass.callWS; let starts = 0;
  env.card._hass.callWS = (message) => {
    if (message.type.endsWith("listen_start") && ++starts === 1) return start.promise;
    return base(message);
  };
  const original = env.card._toggle(); await flush();
  env.card.setConfig({ config_entry_id: "two", station_id: "gate_main", mode: "listen" });
  await env.card._toggle();
  start.resolve({ listener_id: "late-original", station_id: "gate_main", generation: 7, sample_rate: 8000, channels: 1, sample_format: "s16le" });
  await original;
  assert.deepEqual(env.calls.find((message) => message.listener_id === "late-original"), { type: "doorfast/audio/listen_stop", config_entry_id: "one", listener_id: "late-original" });
  assert.equal(env.card._status, "listening");
  assert.equal(env.contexts[0].closeCount, 1);
  assert.equal(env.contexts[1].closeCount, 0);
  await env.card._stop();
});

test("late capture identity and pending original backend stop cannot reset the replacement", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const start = deferred(); const stop = deferred(); const base = env.card._hass.callWS; let starts = 0;
  env.card._hass.callWS = (message) => {
    if (message.type.endsWith("/start") && ++starts === 1) return start.promise;
    if (message.type.endsWith("listen_stop") && message.listener_id === "listener-1") { env.calls.push(message); return stop.promise; }
    return base(message);
  };
  const original = env.card._toggle(); await flush();
  const stopping = env.card._stop(); await flush();
  assert.equal(env.card._status, "idle");
  await env.card._toggle();
  start.resolve({ capture_id: "late-capture", state: "active", sequence: 0 }); await original;
  assert.equal(env.card._status, "active");
  stop.resolve({ state: "idle" }); await stopping;
  assert.equal(env.card._status, "active");
  assert.equal(env.contexts[1].closeCount, 0);
  assert.equal(env.calls.filter((message) => message.capture_id === "late-capture" && message.type.endsWith("/stop")).length, 1);
  await env.card._stop();
});

test("card-owned singleflight survives controller replacement and drops old audio", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup("listen"); const read = deferred();
  env.reads.push(read.promise, { state: "chunk", listener_id: "listener-2", generation: 7, previous_revision: 0, revision: 1, discontinuity: true, pcm: "AIA=" });
  await env.card._toggle(); t.mock.timers.tick(0); await flush();
  assert.equal(env.calls.filter((message) => message.type.endsWith("listen_read")).length, 1);
  await env.card._stop(); await env.card._toggle(); t.mock.timers.tick(0); await flush();
  assert.equal(env.calls.filter((message) => message.type.endsWith("listen_read")).length, 1, "no second WS request until old one settles");
  read.resolve({ state: "chunk", listener_id: "listener-1", generation: 7, previous_revision: 0, revision: 1, discontinuity: false, pcm: "AIA=" });
  await flush();
  assert.equal(env.contexts[0].sources.length, 0);
  assert.equal(env.contexts[1].sources.length, 1);
  assert.equal(env.calls.filter((message) => message.type.endsWith("listen_read")).length, 2);
  assert.equal(env.card._status, "listening");
  await env.card._stop();
});

test("late uplink error, old track-ended and old worklet callbacks cannot stop replacement audio", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const env = setup(); const submit = deferred(); const base = env.card._hass.callWS;
  env.card._hass.callWS = (message) => message.type.endsWith("submit") ? submit.promise : base(message);
  await env.card._toggle();
  const old = env.card._session; const callback = old.worklet.port.onmessage; const ended = env.tracks[0].handlers.ended[0];
  callback({ data: { type: "frame", frame: new ArrayBuffer(320) } });
  await env.card._stop();
  const replacement = new Target(); replacement.stopped = 0; replacement.stop = () => { replacement.stopped++; };
  navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [replacement] });
  await env.card._toggle();
  submit.reject(new Error("old submission failed")); await flush();
  ended(); callback({ data: { type: "error" } }); await flush();
  assert.equal(env.card._status, "active");
  assert.equal(replacement.stopped, 0);
  assert.equal(env.contexts[1].closeCount, 0);
  assert.equal(env.contexts[0].closeCount, 1);
  await env.card._stop();
});

test("PTT keeps pointer and Space/Enter hold semantics without a configured station", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  for (const input of ["pointer", "Space", "Enter"]) {
    const env = setup("ptt"); env.card.setConfig({ config_entry_id: "one" });
    const button = env.card.shadowRoot.querySelector("button");
    button.dispatch(input === "pointer" ? "pointerdown" : "keydown", { code: input, pointerId: 1, repeat: false }); await flush();
    assert.equal(env.card._status, "active");
    assert.equal(env.calls.some((message) => message.type.endsWith("listen_start")), false);
    assert.deepEqual(env.calls.find((message) => message.type.endsWith("/start")), { type: "doorfast/audio/start", config_entry_id: "one" });
    button.dispatch(input === "pointer" ? "pointerup" : "keyup", { code: input }); await flush();
    assert.equal(env.card._status, "idle"); assert.equal(env.contexts[0].closeCount, 1);
  }
});

for (const event of ["blur", "hidden", "disconnected", "track-ended", "detach", "configuration"]) {
  test(`${event} clears both directions and restoring visibility/connection does not restart`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const env = setup(); await env.card._toggle();
    if (event === "blur") env.card._onBlur();
    if (event === "hidden") { document.hidden = true; env.card._onVisibility(); }
    if (event === "disconnected") env.card._onDisconnected();
    if (event === "track-ended") env.tracks[0].dispatch("ended");
    if (event === "detach") env.card.disconnectedCallback();
    if (event === "configuration") env.card.setConfig({ config_entry_id: "two", station_id: "gate_main", mode: "listen" });
    await flush();
    document.hidden = false; env.card._onVisibility(); env.card.hass = env.card._hass; await flush();
    assert.equal(env.card._status, "idle");
    assert.equal(env.micCalls, 1); assert.equal(env.contexts[0].closeCount, 1); assert.equal(env.tracks[0].stopped, 1);
    assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, 1);
    assert.equal(env.calls.filter((message) => message.type.endsWith("/stop")).length, 1);
  });
}

for (const invalid of [
  { state: "retry" }, { listener_id: "other" }, { generation: 8 }, { generation: Number.MAX_SAFE_INTEGER + 1 },
  { previous_revision: -1 }, { revision: Number.MAX_SAFE_INTEGER + 1 }, { revision: 0 }, { discontinuity: 1 },
  { pcm: "" }, { pcm: "AA==" }, { pcm: btoa("a".repeat(3202)) }, { pcm: "!!!!" }, { pcm: "AIB=" },
]) {
  test(`invalid downlink ${Object.keys(invalid)[0]} is rejected before native output`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const env = setup("listen");
    env.reads.push({ state: "chunk", listener_id: "listener-1", generation: 7, previous_revision: 0, revision: 1, discontinuity: false, pcm: "AIA=", ...invalid });
    await env.card._toggle(); t.mock.timers.tick(0); await flush();
    assert.equal(env.card._status, "error: Audio connection lost");
    assert.equal(env.contexts[0].sources.length, 0); assert.equal(env.contexts[0].closeCount, 1);
    assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, 1);
  });
}

for (const step of ["resume", "worklet", "listen_start", "capture_start", "media_source", "worklet_node", "source_connect", "output"]) {
  test(`failure at ${step} releases all resources acquired before that step`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const env = setup(); const Original = AudioContext; const base = env.card._hass.callWS;
    globalThis.AudioContext = class extends Original {
      constructor() { super(); if (step === "worklet") this.audioWorklet.addModule = async () => { throw new Error("module failure"); }; }
      async resume() { if (step === "resume") throw new Error("resume failure"); return super.resume(); }
      createMediaStreamSource() {
        if (step === "media_source") throw new Error("source failure");
        const source = super.createMediaStreamSource(); if (step === "source_connect") source.connect = () => { throw new Error("connect failure"); }; return source;
      }
      createBufferSource() { if (step === "output") throw new Error("output failure"); return super.createBufferSource(); }
    };
    if (step === "worklet_node") globalThis.AudioWorkletNode = class { constructor() { throw new Error("node failure"); } };
    env.card._hass.callWS = (message) => {
      if ((step === "listen_start" && message.type.endsWith("listen_start")) || (step === "capture_start" && message.type.endsWith("/start"))) throw new Error("private backend body");
      return base(message);
    };
    if (step === "output") env.reads.push({ state: "chunk", listener_id: "listener-1", generation: 7, previous_revision: 0, revision: 1, discontinuity: false, pcm: "AIA=" });
    await env.card._toggle(); if (step === "output") t.mock.timers.tick(0); await flush();
    assert.equal(env.card._status, "error: Audio connection lost");
    assert.equal(env.contexts[0].closeCount, 1); assert.equal(env.tracks[0].stopped, 1);
    assert.equal(env.calls.filter((message) => message.type.endsWith("listen_stop")).length, step === "listen_start" ? 0 : 1);
    assert.equal(env.calls.filter((message) => message.type.endsWith("/stop")).length, ["media_source", "worklet_node", "source_connect", "output"].includes(step) ? 1 : 0);
  });
}
