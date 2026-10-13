import assert from "node:assert/strict";
import test from "node:test";

import {
  FloatToPcm16Resampler,
  PcmFrameAssembler,
} from "../../custom_components/doorfast/frontend/pcm-dsp.mjs";
import { PcmBatchSender, PcmAudioPlayback } from "../../custom_components/doorfast/frontend/audio-stream.mjs";

function sine(rate, frequency, seconds, amplitude = 0.5) {
  return Float32Array.from(
    { length: Math.round(rate * seconds) },
    (_, index) => amplitude * Math.sin((2 * Math.PI * frequency * index) / rate),
  );
}

test("streaming resampler produces exact 8 kHz sample counts at common variable rates", () => {
  for (const rate of [44100, 48000, 96000]) {
    const resampler = new FloatToPcm16Resampler(rate);
    const input = sine(rate, 440, 0.1);
    const output = [];
    for (let offset = 0; offset < input.length; offset += 137) {
      output.push(...resampler.process(input.subarray(offset, offset + 137)));
    }
    assert.equal(output.length, 800);
    assert.ok(Math.max(...output) > 15000);
    assert.ok(Math.min(...output) < -15000);
  }
});

test("low-pass resampling preserves speech and attenuates aliases", () => {
  const rate = 48000;
  const low = new FloatToPcm16Resampler(rate).process(sine(rate, 1000, 1)).slice(1000);
  const rms = (samples) => Math.sqrt(samples.reduce((sum, value) => sum + value ** 2, 0) / samples.length);
  const reference = rms(low);
  for (const frequency of [3000, 3400]) {
    const speech = new FloatToPcm16Resampler(rate).process(sine(rate, frequency, 1)).slice(1000);
    assert.ok(rms(speech) > reference * 0.69, `${frequency} Hz lost more than 3.3 dB`);
  }
  const nyquist = new FloatToPcm16Resampler(rate).process(sine(rate, 4000, 1)).slice(1000);
  assert.ok(rms(nyquist) < reference * 0.2, "4 kHz was not attenuated by 14 dB");
  for (const frequency of [6000, 7000, 10000, 12000]) {
    const high = new FloatToPcm16Resampler(rate).process(sine(rate, frequency, 1)).slice(1000);
    assert.ok(rms(high) < reference * 0.1, `${frequency} Hz was not attenuated by 20 dB`);
  }
});

test("resampling is chunk independent, bounded in drift, and reset clears history", () => {
  const rate = 44100;
  const input = sine(rate, 733, 10);
  const wholeResampler = new FloatToPcm16Resampler(rate);
  const whole = [...wholeResampler.process(input)];
  const chunkedResampler = new FloatToPcm16Resampler(rate);
  const chunked = [];
  for (let offset = 0; offset < input.length; offset += 127) {
    chunked.push(...chunkedResampler.process(input.subarray(offset, offset + 127)));
  }
  assert.ok(Math.abs(whole.length - 80000) <= 1);
  assert.deepEqual(chunked, whole);
  chunkedResampler.process(Float32Array.from({ length: 200 }, () => 1));
  chunkedResampler.reset();
  const silence = chunkedResampler.process(new Float32Array(100));
  assert.ok([...silence].every((sample) => sample === 0));
});

test("conversion clips finite input and treats nonfinite input as silence", () => {
  const hot = new FloatToPcm16Resampler(8000).process(Float32Array.from({ length: 80 }, () => 4));
  assert.equal(hot.at(-1), 32767);
  const invalid = new FloatToPcm16Resampler(8000).process(Float32Array.of(NaN, Infinity, -Infinity, 0));
  assert.deepEqual([...invalid], [0, 0, 0, 0]);
});

test("frame assembler emits only exact 320-byte little-endian frames", () => {
  const assembler = new PcmFrameAssembler();
  const first = assembler.push(Int16Array.from({ length: 159 }, (_, i) => i - 80));
  assert.deepEqual(first, []);
  const frames = assembler.push(Int16Array.of(123, -456, 789));
  assert.equal(frames.length, 1);
  assert.equal(frames[0].byteLength, 320);
  const view = new DataView(frames[0]);
  assert.equal(view.getInt16(318, true), 123);
  assert.equal(assembler.pendingSamples, 2);
});

test("batch sender keeps one request in flight and batches at most five frames", async () => {
  const pending = [];
  const calls = [];
  const sender = new PcmBatchSender((message) => {
    calls.push(message);
    return new Promise((resolve) => pending.push(resolve));
  }, "entry", "capture", { maxQueuedFrames: 10 });
  const frames = Array.from({ length: 8 }, (_, value) => new Uint8Array(320).fill(value).buffer);
  sender.enqueue(frames);
  await Promise.resolve();
  assert.equal(calls.length, 1);
  assert.equal(Buffer.from(calls[0].pcm, "base64").length, 1600);
  pending.shift()({ accepted_frames: 5 });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(calls.length, 2);
  assert.equal(Buffer.from(calls[1].pcm, "base64").length, 960);
  pending.shift()({ accepted_frames: 3 });
  await sender.drain();
});

test("bounded sender drops oldest queued live frames and never replays a failed batch", async () => {
  const pending = [];
  const calls = [];
  const sender = new PcmBatchSender((message) => {
    calls.push(Buffer.from(message.pcm, "base64"));
    return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  }, "entry", "capture", { maxQueuedFrames: 5 });
  sender.enqueue([new Uint8Array(320).fill(1).buffer]);
  await Promise.resolve();
  sender.enqueue(Array.from({ length: 7 }, (_, i) => new Uint8Array(320).fill(i + 2).buffer));
  assert.equal(sender.droppedFrames, 2);
  pending.shift().reject(new Error("connection lost"));
  await assert.rejects(sender.drain(), /connection lost/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 1);
  assert.equal(sender.queuedFrames, 0);
});

test("closing while a request is pending prevents late completion from sending queued audio", async () => {
  let resolve;
  const calls = [];
  const sender = new PcmBatchSender((message) => {
    calls.push(message);
    return new Promise((done) => { resolve = done; });
  }, "entry", "capture");
  sender.enqueue([new ArrayBuffer(320)]);
  sender.enqueue([new ArrayBuffer(320)]);
  sender.close();
  resolve({ accepted_frames: 1 });
  await sender.drain();
  assert.equal(calls.length, 1);
  assert.equal(sender.queuedFrames, 0);
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

async function flushMicrotasks() {
  for (let count = 0; count < 12; count += 1) await Promise.resolve();
}

// Replace only the external timer boundary: the poller still performs all work.
function controlledTimers(t) {
  let id = 0;
  const pending = new Map();
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    pending.set(++id, { callback, delay });
    return id;
  });
  t.mock.method(globalThis, "clearTimeout", (timer) => pending.delete(timer));
  return {
    pending,
    async next() {
      assert.equal(pending.size, 1, "exactly one polling timer is allowed");
      const [timer, { callback, delay }] = pending.entries().next().value;
      pending.delete(timer);
      callback();
      await flushMicrotasks();
      return delay;
    },
  };
}

test("playback polling stops safely on fetch rejection", async (t) => {
  const timers = controlledTimers(t);
  let errors = 0;
  const playback = new PcmAudioPlayback(async () => { throw new Error("gone"); }, {
    onError: () => { errors += 1; }, intervalMs: 1,
  });
  playback.start({ station_id: "front", generation: 7 });
  await timers.next();
  assert.equal(errors, 1);
  assert.equal(playback.timer, null);
  assert.equal(timers.pending.size, 0);
});

// Catches listen_read empty results being forwarded as audio or slow chunk reads.
test("listen_read chunks poll immediately while empty results wait 50 ms", async (t) => {
  const timers = controlledTimers(t);
  const chunk = {
    state: "chunk", listener_id: "listener", generation: 7,
    previous_revision: 0, revision: 1, pcm: new ArrayBuffer(1600), discontinuity: false,
  };
  const identity = { station_id: "front", generation: 7 };
  const results = [chunk, { state: "empty" }, null, new ArrayBuffer(0)];
  const played = [];
  const playback = new PcmAudioPlayback(async () => results.shift(), {
    playChunk: async (value, currentIdentity) => played.push([value, currentIdentity]),
  });
  t.after(() => playback.stop());
  playback.start(identity);
  assert.equal(await timers.next(), 0);
  assert.equal(await timers.next(), 0);
  assert.equal(await timers.next(), 50);
  assert.equal(await timers.next(), 50);
  assert.deepEqual(played, [[chunk, identity]]);
  assert.equal([...timers.pending.values()][0].delay, 50);
  playback.stop();
  assert.equal(timers.pending.size, 0);
});

// Catches a replacement start overlapping an unabortable prior listen_read.
test("playback keeps one fetch in flight across stop and repeated start", async (t) => {
  const timers = controlledTimers(t);
  const reads = [];
  const played = [];
  const playback = new PcmAudioPlayback((identity) => {
    const read = deferred();
    reads.push({ ...read, identity });
    return read.promise;
  }, { playChunk: (chunk) => played.push(chunk) });
  t.after(() => playback.stop());
  playback.start({ generation: 1 });
  await timers.next();
  assert.equal(reads.length, 1);
  assert.equal(timers.pending.size, 0);
  playback.stop();
  playback.start({ generation: 2 });
  playback.start({ generation: 3 });
  if (timers.pending.size) await timers.next();
  assert.equal(reads.length, 1, "old in-flight read still owns the request slot");
  reads[0].resolve({ state: "chunk", pcm: new ArrayBuffer(1600) });
  await flushMicrotasks();
  assert.deepEqual(played, []);
  await timers.next();
  assert.equal(reads.length, 2);
  assert.deepEqual(reads[1].identity, { generation: 3 });
  reads[1].resolve({ state: "empty" });
  await flushMicrotasks();
  playback.stop();
  assert.equal(timers.pending.size, 0);
});

// Catches a stopped fetch's late rejection calling stop on the new epoch.
test("late fetch errors cannot stop or report errors for a new playback epoch", async (t) => {
  const timers = controlledTimers(t);
  const oldRead = deferred();
  const errors = [];
  let reads = 0;
  const playback = new PcmAudioPlayback(() => {
    reads += 1;
    return reads === 1 ? oldRead.promise : Promise.resolve({ state: "empty" });
  }, { onError: (error) => errors.push(error) });
  t.after(() => playback.stop());
  playback.start();
  await timers.next();
  playback.stop();
  playback.start();
  oldRead.reject(new Error("old read failed"));
  await flushMicrotasks();
  assert.deepEqual(errors, []);
  await timers.next();
  assert.equal(reads, 2);
  assert.equal(timers.pending.size, 1);
});

// Catches old asynchronous playback completions scheduling timers or stopping a new epoch.
for (const outcome of ["resolve", "reject"]) {
  test(`late playChunk ${outcome} preserves the new playback epoch`, async (t) => {
    const timers = controlledTimers(t);
    const oldPlay = deferred();
    const errors = [];
    let reads = 0;
    const playback = new PcmAudioPlayback(async () => {
      reads += 1;
      return reads === 1 ? new ArrayBuffer(320) : { state: "empty" };
    }, {
      playChunk: () => oldPlay.promise,
      onError: (error) => errors.push(error),
    });
    t.after(() => playback.stop());
    playback.start();
    await timers.next();
    playback.stop();
    playback.start();
    if (outcome === "reject") oldPlay.reject(new Error("old output failed"));
    else oldPlay.resolve();
    await flushMicrotasks();
    assert.deepEqual(errors, []);
    await timers.next();
    assert.equal(reads, 2);
    assert.equal(timers.pending.size, 1);
  });
}

// Catches a stopped in-flight fetch reviving polling after it settles.
test("stop during a pending read leaves no timer and never plays its late audio", async (t) => {
  const timers = controlledTimers(t);
  const read = deferred();
  const played = [];
  const playback = new PcmAudioPlayback(() => read.promise, { playChunk: (chunk) => played.push(chunk) });
  playback.start();
  await timers.next();
  playback.stop();
  read.resolve(new ArrayBuffer(320));
  await flushMicrotasks();
  assert.deepEqual(played, []);
  assert.equal(timers.pending.size, 0);
  assert.equal(playback.timer, null);
});

// Catches stale call identity audio reaching the output.
test("isCurrent prevents playback of a superseded call identity", async (t) => {
  const timers = controlledTimers(t);
  const played = [];
  const playback = new PcmAudioPlayback(async () => new ArrayBuffer(320), {
    isCurrent: () => false, playChunk: (chunk) => played.push(chunk),
  });
  t.after(() => playback.stop());
  playback.start();
  await timers.next();
  assert.deepEqual(played, []);
});
