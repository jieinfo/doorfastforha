export const OUTPUT_RATE = 8000;
export const FRAME_SAMPLES = 160;
export const FRAME_BYTES = FRAME_SAMPLES * 2;

function lowPassTaps(inputRate, cutoff = 3900, length = 127) {
  const center = (length - 1) / 2;
  const normalized = Math.min(cutoff, inputRate * 0.45) / inputRate;
  const taps = new Float64Array(length);
  let total = 0;
  for (let index = 0; index < length; index += 1) {
    const offset = index - center;
    const sinc = offset === 0
      ? 2 * normalized
      : Math.sin(2 * Math.PI * normalized * offset) / (Math.PI * offset);
    const window = 0.42
      - 0.5 * Math.cos((2 * Math.PI * index) / (length - 1))
      + 0.08 * Math.cos((4 * Math.PI * index) / (length - 1));
    taps[index] = sinc * window;
    total += taps[index];
  }
  for (let index = 0; index < length; index += 1) taps[index] /= total;
  return taps;
}

export class FloatToPcm16Resampler {
  constructor(inputRate, outputRate = OUTPUT_RATE) {
    if (!Number.isFinite(inputRate) || inputRate < outputRate) {
      throw new Error("Microphone sample rate must be at least 8 kHz");
    }
    this.ratio = inputRate / outputRate;
    this.taps = lowPassTaps(inputRate);
    this.reset();
  }

  reset() {
    this.weight = 0;
    this.sum = 0;
    this.history = new Float64Array(this.taps.length);
    this.historyIndex = 0;
  }

  process(samples) {
    const output = [];
    for (let index = 0; index < samples.length; index += 1) {
      const input = Number.isFinite(samples[index]) ? Math.max(-1, Math.min(1, samples[index])) : 0;
      this.history[this.historyIndex] = input;
      let value = 0;
      let historyIndex = this.historyIndex;
      for (let tap = 0; tap < this.taps.length; tap += 1) {
        value += this.taps[tap] * this.history[historyIndex];
        historyIndex = historyIndex === 0 ? this.history.length - 1 : historyIndex - 1;
      }
      this.historyIndex = (this.historyIndex + 1) % this.history.length;
      let remaining = 1;
      while (remaining > 1e-12) {
        const take = Math.min(remaining, this.ratio - this.weight);
        this.sum += value * take;
        this.weight += take;
        remaining -= take;
        if (this.weight + 1e-12 >= this.ratio) {
          const filtered = Math.max(-1, Math.min(1, this.sum / this.ratio));
          output.push(filtered < 0 ? Math.round(filtered * 32768) : Math.round(filtered * 32767));
          this.weight = 0;
          this.sum = 0;
        }
      }
    }
    return Int16Array.from(output);
  }
}

export class PcmFrameAssembler {
  constructor() {
    this.samples = [];
  }

  get pendingSamples() {
    return this.samples.length;
  }

  reset() {
    this.samples.length = 0;
  }

  push(samples) {
    this.samples.push(...samples);
    const frames = [];
    while (this.samples.length >= FRAME_SAMPLES) {
      const frame = new ArrayBuffer(FRAME_BYTES);
      const view = new DataView(frame);
      const values = this.samples.splice(0, FRAME_SAMPLES);
      values.forEach((value, index) => view.setInt16(index * 2, value, true));
      frames.push(frame);
    }
    return frames;
  }
}
