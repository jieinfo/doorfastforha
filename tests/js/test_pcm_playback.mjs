import assert from "node:assert/strict";
import test from "node:test";
import { PcmAudioPlayback } from "../../custom_components/doorfast/frontend/audio-stream.mjs";

import { pcm16ToFloat32, PcmAudioOutput } from "../../custom_components/doorfast/frontend/pcm-playback.mjs";

// Catches wrong endian reads and asymmetric PCM normalization.
test("PCM conversion preserves little-endian signed sample extrema", () => {
  const bytes = Uint8Array.of(0, 128, 255, 127, 0, 0, 0, 64);
  assert.deepEqual([...pcm16ToFloat32(bytes.buffer)], [-1, 32767 / 32768, 0, 0.5]);
});

// Catches empty, truncated, oversized, and non-buffer chunks reaching playback.
test("PCM conversion rejects invalid byte lengths and accepts the maximum block", () => {
  for (const size of [0, 1, 3, 3199, 3201, 3202]) {
    assert.throws(() => pcm16ToFloat32(new ArrayBuffer(size)), /Invalid PCM/);
  }
  assert.throws(() => pcm16ToFloat32(new Uint8Array(2)), /Invalid PCM/);
  assert.equal(pcm16ToFloat32(new ArrayBuffer(2)).length, 1);
  assert.equal(pcm16ToFloat32(new ArrayBuffer(3200)).length, 1600);
});

class FakeAudioContext {
  constructor(currentTime, sampleRate = 48000) {
    this.currentTime = currentTime;
    this.sampleRate = sampleRate;
    this.destination = {};
    this.sources = [];
    this.buffers = [];
  }
  createBuffer(channels, samples, sampleRate) {
    const data = new Float32Array(samples);
    const buffer = { channels, length: samples, sampleRate, getChannelData: () => data };
    this.buffers.push(buffer);
    return buffer;
  }
  createBufferSource() {
    const source = {
      stopped: 0, disconnected: 0,
      start(time) { this.startAt = time; },
      stop() { this.stopped += 1; },
      connect(destination) { this.destination = destination; },
      disconnect() { this.disconnected += 1; },
      end() { this.onended?.(); },
    };
    this.sources.push(source);
    return source;
  }
}

function near(actual, expected) {
  assert.ok(Math.abs(actual - expected) <= 1e-9, `${actual} != ${expected}`);
}

// Catches context-rate PCM reinterpretation and repeated startup gaps.
test("PCM output schedules continuous sample-clock audio at an 8 kHz source rate", () => {
  for (const rate of [44100, 48000]) {
    const context = new FakeAudioContext(2, rate);
    const output = new PcmAudioOutput(context);
    const bytes = new Uint8Array(1600);
    bytes.set([0, 128, 255, 127]);
    output.enqueue(bytes.buffer);
    context.currentTime = 2.05;
    output.enqueue(new ArrayBuffer(1600));
    near(context.sources[0].startAt, 2.12);
    near(context.sources[1].startAt, 2.22);
    assert.equal(context.sources[0].destination, context.destination);
    assert.equal(context.buffers[0].channels, 1);
    assert.equal(context.buffers[0].length, 800);
    assert.equal(context.buffers[0].sampleRate, 8000);
    assert.deepEqual([...context.buffers[0].getChannelData(0).slice(0, 2)], [-1, 32767 / 32768]);
  }
});

// Catches startup being excluded from the future-audio cap.
test("maximum PCM chunks fit the cap including startup", () => {
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  output.enqueue(new ArrayBuffer(3200));
  near(context.sources[0].startAt, 2.1);
  near(context.sources[0].startAt + 0.2 - context.currentTime, 0.3);
  assert.equal(context.buffers[0].length, 1600);
});

// Catches shortening a chunk or violating a caller's smaller future-audio cap.
test("PCM output rejects a chunk that cannot fit a smaller cap", () => {
  const small = new FakeAudioContext(2);
  const capped = new PcmAudioOutput(small, { maxBufferMs: 90 });
  assert.throws(() => capped.enqueue(new ArrayBuffer(1600)), /buffer cap/);
  assert.equal(small.sources.length, 0);
});

// Catches accumulated scheduling latency exceeding the 300 ms budget.
test("backlog resets unplayed nodes while preserving the currently playing block", () => {
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  output.enqueue(new ArrayBuffer(1600));
  context.currentTime = 2.13;
  output.enqueue(new ArrayBuffer(1600));
  output.enqueue(new ArrayBuffer(1600));
  output.enqueue(new ArrayBuffer(1600));
  near(context.sources[3].startAt, 2.25);
  assert.equal(context.sources[0].stopped, 0);
  assert.equal(context.sources[1].stopped, 1);
  assert.equal(context.sources[2].stopped, 1);
  near(context.sources[3].startAt + 0.1 - context.currentTime, 0.22);
});

// Catches scheduling in the past after a missing block.
test("underflow reestablishes the startup lead", () => {
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  output.enqueue(new ArrayBuffer(1600));
  context.currentTime = 3;
  output.enqueue(new ArrayBuffer(1600));
  near(context.sources[1].startAt, 3.12);
});

// Catches playing stale queued audio after listener discontinuity.
test("discontinuity clears unplayed audio and restarts within the cap", () => {
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  output.enqueue(new ArrayBuffer(1600));
  context.currentTime = 2.05;
  output.enqueue(new ArrayBuffer(3200), { discontinuity: true });
  assert.equal(context.sources[0].stopped, 1);
  near(context.sources[1].startAt, 2.15);
  near(context.sources[1].startAt + 0.2 - context.currentTime, 0.3);
});

// Catches sources surviving teardown or duplicate stop calls.
test("PCM output stop releases every node once and permits fresh startup", () => {
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  output.enqueue(new ArrayBuffer(1600));
  context.currentTime = 2.13;
  output.enqueue(new ArrayBuffer(1600));
  output.stop();
  output.stop();
  assert.ok(context.sources.every((source) => source.stopped === 1 && source.disconnected === 1));
  context.currentTime = 3;
  output.enqueue(new ArrayBuffer(1600));
  near(context.sources[2].startAt, 3.12);
});

// Catches ended nodes accumulating or being stopped again during teardown.
test("ended nodes disconnect and leave the output's active set", () => {
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  output.enqueue(new ArrayBuffer(1600));
  const source = context.sources[0];
  source.end();
  assert.equal(source.disconnected, 1);
  output.stop();
  assert.equal(source.stopped, 0);
  assert.equal(source.disconnected, 1);
});

// Catches native output options being ignored, discontinuity lost, or teardown not stopping audio.
test("poller enqueues decoded PCM into native output and stops its nodes", async (t) => {
  const timers = new Map();
  let id = 0;
  t.mock.method(globalThis, "setTimeout", (callback) => { timers.set(++id, callback); return id; });
  t.mock.method(globalThis, "clearTimeout", (timer) => timers.delete(timer));
  const context = new FakeAudioContext(2);
  const output = new PcmAudioOutput(context);
  const results = [
    { state: "chunk", pcm: new ArrayBuffer(1600), discontinuity: false },
    { state: "chunk", pcm: new ArrayBuffer(3200), discontinuity: true },
  ];
  const playback = new PcmAudioPlayback(async () => results.shift(), { output });
  t.after(() => playback.stop());
  playback.start();
  for (let count = 0; count < 2; count += 1) {
    assert.equal(timers.size, 1);
    const [timer, callback] = timers.entries().next().value;
    timers.delete(timer);
    callback();
    for (let step = 0; step < 12; step += 1) await Promise.resolve();
  }
  assert.equal(context.sources.length, 2);
  assert.equal(context.sources[0].stopped, 1);
  near(context.sources[1].startAt, 2.1);
  assert.equal(context.buffers[1].length, 1600);
  playback.stop();
  assert.equal(context.sources[1].stopped, 1);
  assert.equal(timers.size, 0);
});

// Catches invalid timing budgets bypassing bounds or scheduling negative times.
test("PCM output rejects nonfinite or negative timing options", () => {
  for (const options of [
    { startupBufferMs: -1 }, { startupBufferMs: NaN }, { startupBufferMs: Infinity },
    { maxBufferMs: -1 }, { maxBufferMs: 0 }, { maxBufferMs: NaN }, { maxBufferMs: Infinity },
  ]) {
    assert.throws(() => new PcmAudioOutput(new FakeAudioContext(2), options), /timing/);
  }
});

// Catches a caller expanding the global 300 ms audio latency budget.
test("PCM output rejects a configured cap above 300 ms", () => {
  assert.throws(() => new PcmAudioOutput(new FakeAudioContext(2), { maxBufferMs: 301 }), /timing/);
});
