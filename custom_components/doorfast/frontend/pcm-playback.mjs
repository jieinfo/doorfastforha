/** Decode signed little-endian PCM16 samples into Web Audio amplitudes. */
export function pcm16ToFloat32(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength === 0
      || buffer.byteLength > 3200 || buffer.byteLength % 2 !== 0) {
    throw new Error("Invalid PCM buffer");
  }
  const view = new DataView(buffer);
  const samples = new Float32Array(buffer.byteLength / 2);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 32768;
  }
  return samples;
}

/** Schedule mono 8 kHz PCM on the caller-owned AudioContext clock. */
export class PcmAudioOutput {
  constructor(context, { startupBufferMs = 120, maxBufferMs = 300 } = {}) {
    if (!Number.isFinite(startupBufferMs) || startupBufferMs < 0
        || !Number.isFinite(maxBufferMs) || maxBufferMs <= 0 || maxBufferMs > 300) {
      throw new Error("Invalid PCM timing options");
    }
    this.context = context;
    this.startupBufferMs = startupBufferMs;
    this.maxBufferMs = maxBufferMs;
    this.nextStart = null;
    this.sources = new Map();
  }

  enqueue(buffer, { discontinuity = false } = {}) {
    const samples = pcm16ToFloat32(buffer);
    const duration = samples.length / 8000;
    const maxBuffer = this.maxBufferMs / 1000;
    if (duration > maxBuffer) throw new Error("PCM chunk exceeds buffer cap");
    const lead = Math.min(this.startupBufferMs / 1000, maxBuffer - duration);
    const now = this.context.currentTime;
    if (discontinuity || (this.nextStart !== null && this.nextStart < now)
        || (this.nextStart !== null && this.nextStart + duration - now > maxBuffer + 1e-9)) {
      let retainedEnd = now;
      for (const [source, { startAt, endAt }] of this.sources) {
        // Preserve playing tails only when the newest whole chunk can follow within the cap.
        if (startAt > now || endAt + duration - now > maxBuffer + 1e-9) {
          this.#release(source, true);
        } else {
          retainedEnd = Math.max(retainedEnd, endAt);
        }
      }
      this.nextStart = Math.max(now + lead, retainedEnd);
    }
    const startAt = this.nextStart ?? now + lead;
    const audioBuffer = this.context.createBuffer(1, samples.length, 8000);
    audioBuffer.getChannelData(0).set(samples);
    const source = this.context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.context.destination);
    this.sources.set(source, { startAt, endAt: startAt + duration });
    source.onended = () => this.#release(source);
    source.start(startAt);
    this.nextStart = startAt + duration;
  }

  #release(source, stop = false) {
    if (!this.sources.delete(source)) return;
    source.onended = null;
    if (stop) source.stop();
    source.disconnect();
  }

  stop() {
    for (const source of this.sources.keys()) this.#release(source, true);
    this.nextStart = null;
  }
}
