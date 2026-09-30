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
    this.stationId = options.stationId ?? null;
    this.generation = options.generation ?? null;
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
      const message = {
        type: "doorfast/audio/submit",
        config_entry_id: this.configEntryId,
        capture_id: this.captureId,
        pcm: encodeFrames(frames),
      };
      if (this.stationId !== null) message.station_id = this.stationId;
      if (this.generation !== null) message.generation = this.generation;
      await this.callWS(message);
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

/** Poll existing latest-audio snapshots and discard stale call generations. */
export class PcmAudioPlayback {
  constructor(fetchChunk, options = {}) {
    this.fetchChunk = fetchChunk;
    this.playChunk = options.playChunk ?? (async () => {});
    this.intervalMs = options.intervalMs ?? 250;
    this.isCurrent = options.isCurrent ?? (() => true);
    this.timer = null;
    this.epoch = 0;
  }

  start(identity = {}) {
    this.stop();
    const epoch = ++this.epoch;
    const poll = async () => {
      if (epoch !== this.epoch) return;
      const chunk = await this.fetchChunk(identity);
      if (epoch !== this.epoch) return;
      if (chunk && this.isCurrent(identity)) await this.playChunk(chunk, identity);
      this.timer = setTimeout(poll, this.intervalMs);
    };
    this.timer = setTimeout(poll, 0);
  }

  stop() {
    this.epoch += 1;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}
