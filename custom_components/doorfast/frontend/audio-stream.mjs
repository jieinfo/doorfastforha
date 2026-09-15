const FRAME_BYTES = 320;

function encodeFrames(frames) {
  const bytes = new Uint8Array(frames.length * FRAME_BYTES);
  frames.forEach((frame, index) => bytes.set(new Uint8Array(frame), index * FRAME_BYTES));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export class PcmBatchSender {
  constructor(callWS, configEntryId, captureId, options = {}) {
    this.callWS = callWS;
    this.configEntryId = configEntryId;
    this.captureId = captureId;
    this.maxQueuedFrames = options.maxQueuedFrames ?? 5;
    this.onError = options.onError ?? (() => {});
    this.queue = [];
    this.droppedFrames = 0;
    this.epoch = 0;
    this.pumpPromise = null;
    this.lastError = null;
  }

  get queuedFrames() { return this.queue.length; }

  enqueue(frames) {
    if (!frames.every((frame) => frame.byteLength === FRAME_BYTES)) throw new Error("Invalid PCM frame");
    this.queue.push(...frames);
    if (this.queue.length > this.maxQueuedFrames) {
      const dropped = this.queue.length - this.maxQueuedFrames;
      this.queue.splice(0, dropped);
      this.droppedFrames += dropped;
    }
    if (!this.pumpPromise) {
      const epoch = this.epoch;
      this.pumpPromise = this.#pump(epoch).catch((error) => {
        this.lastError = error;
        this.queue.length = 0;
        this.onError(error);
      }).finally(() => { this.pumpPromise = null; });
    }
  }

  async #pump(epoch) {
    while (epoch === this.epoch && this.queue.length) {
      const frames = this.queue.splice(0, 5);
      await this.callWS({
        type: "doorfast/audio/submit",
        config_entry_id: this.configEntryId,
        capture_id: this.captureId,
        pcm: encodeFrames(frames),
      });
    }
  }

  close() {
    this.epoch += 1;
    this.queue.length = 0;
  }

  async drain() {
    if (this.pumpPromise) await this.pumpPromise;
    if (this.lastError) throw this.lastError;
  }
}
