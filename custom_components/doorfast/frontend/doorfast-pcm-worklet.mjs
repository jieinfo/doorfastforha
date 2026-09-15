import { FloatToPcm16Resampler, PcmFrameAssembler } from "./pcm-dsp.mjs?v=0.1.0";

class DoorfastPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.resampler = new FloatToPcm16Resampler(sampleRate);
    this.assembler = new PcmFrameAssembler();
  }

  process(inputs) {
    try {
      const channels = inputs[0];
      if (!channels || channels.length === 0) return true;
      const mono = new Float32Array(channels[0].length);
      for (let channel = 0; channel < channels.length; channel += 1) {
        const samples = channels[channel];
        for (let index = 0; index < mono.length; index += 1) mono[index] += samples[index] / channels.length;
      }
      const frames = this.assembler.push(this.resampler.process(mono));
      for (const frame of frames) this.port.postMessage({ type: "frame", frame }, [frame]);
    } catch (error) {
      this.port.postMessage({ type: "error", message: error instanceof Error ? error.message : "Audio processing failed" });
      return false;
    }
    return true;
  }
}

registerProcessor("doorfast-pcm-processor", DoorfastPcmProcessor);
