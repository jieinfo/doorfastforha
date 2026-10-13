import { PcmBatchSender, PcmAudioPlayback } from "./audio-stream.mjs?v=20261012.1";
import { PcmAudioOutput } from "./pcm-playback.mjs?v=20261012.1";
import { audioMessage } from "./audio-messages.mjs?v=20261012.1";

const WORKLET_URL = "/doorfast_static/doorfast-pcm-worklet.mjs?v=20261012.1";

export class DoorfastPttCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._status = "idle";
    this._epoch = 0;
    this._onVisibility = () => { if (document.hidden) this._stop(); };
    this._onBlur = () => this._stop();
  }

  setConfig(config) {
    if (!config?.config_entry_id || typeof config.config_entry_id !== "string") {
      throw new Error("Doorfast PTT requires config_entry_id");
    }
    const mode = config.mode ?? "ptt";
    if (!["ptt", "duplex", "listen"].includes(mode)) throw new Error("Invalid audio mode");
    if (mode !== "ptt" && (typeof config.station_id !== "string" || !/^[a-z][a-z0-9_]{0,31}$/.test(config.station_id))) throw new Error("Audio mode requires valid station_id");
    if (config.generation !== undefined && (!Number.isSafeInteger(config.generation) || config.generation <= 0)) throw new Error("Invalid generation");
    const next = {
      mode,
      config_entry_id: config.config_entry_id,
      station_id: config.station_id,
      generation: config.generation,
      name: config.name ?? "Doorfast Push to Talk",
    };
    if (this._config?.mode === next.mode && this._config?.config_entry_id === next.config_entry_id && this._config?.station_id === next.station_id && this._config?.generation === next.generation && this._config?.name === next.name) return;
    if (this._config) this._stop();
    this._config = next;
    this._render();
  }

  set hass(value) {
    if (this._hass?.connection !== value?.connection) {
      if (this._hass) this._stop();
      this._hass?.connection?.removeEventListener?.("disconnected", this._onDisconnected);
    }
    this._hass = value;
    this._setStatus(this._status);
  }

  getCardSize() { return 2; }

  /** Optional playback lifecycle hook; native output works without injection. */
  setAudioPlayback(playback) {
    this._audioPlayback?.stop?.();
    this._audioPlayback = playback ?? null;
    return this._audioPlayback;
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    window.addEventListener("blur", this._onBlur);
    this._session?.connection?.addEventListener?.("disconnected", this._onDisconnected);
    this._render();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    window.removeEventListener("blur", this._onBlur);
    this._session?.connection?.removeEventListener?.("disconnected", this._onDisconnected);
    this._stop();
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const active = ["active", "starting", "listening"].includes(this._status);
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; text-align: center; }
        button { width: 100%; min-height: 64px; border: 0; border-radius: 16px; font: inherit;
          color: var(--primary-text-color); background: var(--secondary-background-color); touch-action: none; }
        button[aria-pressed="true"] { color: var(--text-primary-color); background: var(--primary-color); }
        button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
        .status { margin-top: 8px; color: var(--secondary-text-color); font-size: 0.9em; }
      </style>
      <ha-card header="${this._escape(this._config.name)}">
        <button type="button" aria-pressed="${active}" aria-label="Hold to talk">${active ? "Release to stop" : "Hold to talk"}</button>
        <div class="status" role="status">${this._escape(this._status)}</div>
      </ha-card>`;
    const button = this.shadowRoot.querySelector("button");
    this._setStatus(this._status);
    if (this._config.mode !== "ptt") {
      button.addEventListener("click", () => this._toggle());
      return;
    }
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      button.setPointerCapture?.(event.pointerId);
      this._start();
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) button.addEventListener(type, () => this._stop());
    button.addEventListener("keydown", (event) => {
      if ((event.code === "Space" || event.code === "Enter") && !event.repeat) {
        event.preventDefault(); this._start();
      }
    });
    button.addEventListener("keyup", (event) => {
      if (event.code === "Space" || event.code === "Enter") { event.preventDefault(); this._stop(); }
    });
  }

  _escape(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  }

  async _start() {
    if (this._status !== "idle" && !this._status.startsWith("error:")) return;
    const config = { ...this._config };
    const continuous = config.mode !== "ptt";
    const canCapture = window.isSecureContext && navigator.mediaDevices?.getUserMedia;
    if (!continuous && !canCapture) { this._setStatus(`error: ${this._message("https")}`); return; }
    const session = { epoch: ++this._epoch, config, callWS: this._hass?.callWS?.bind(this._hass), contextClosed: false, connection: this._hass?.connection };
    this._session = session;
    session.connection?.removeEventListener?.("disconnected", this._onDisconnected);
    this._onDisconnected = session.disconnected = () => this._stopSession(session);
    session.connection?.addEventListener?.("disconnected", session.disconnected);
    this._setStatus("starting");
    try {
      if (!session.callWS) throw new Error("Audio connection unavailable");
      // These calls happen in the initiating click, before any permission/WS await.
      session.context = new AudioContext({ latencyHint: "interactive" });
      const settle = (promise) => Promise.resolve(promise).then((value) => ({ value }), (error) => ({ error }));
      const resume = settle(session.context.resume());
      const capture = config.mode !== "listen" && canCapture;
      const mic = capture ? settle(navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false })) : null;
      // Own late permissions immediately, even if listener creation fails first.
      const permission = mic?.then((result) => {
        if (result.value) {
          session.stream = result.value;
          if (!this._isCurrent(session)) this._releaseStream(session);
          else for (const track of session.stream.getTracks()) track.addEventListener("ended", () => this._stopSession(session), { once: true });
        }
        return result;
      });
      const worklet = capture ? settle(Promise.resolve().then(() => session.context.audioWorklet.addModule(WORKLET_URL))) : null;
      if (continuous) {
        const request = { type: "doorfast/audio/listen_start", config_entry_id: config.config_entry_id, station_id: config.station_id };
        if (config.generation !== undefined) request.generation = config.generation;
        const listener = await session.callWS(request);
        session.listenerId = listener.listener_id;
        if (!this._isCurrent(session)) { await this._cleanup(session); return; }
        if (typeof listener.listener_id !== "string" || !listener.listener_id || listener.station_id !== config.station_id
            || !Number.isSafeInteger(listener.generation) || listener.generation <= 0
            || (config.generation !== undefined && listener.generation !== config.generation)
            || listener.sample_rate !== 8000 || listener.channels !== 1 || listener.sample_format !== "s16le") throw new Error("Invalid listener response");
        session.identity = listener;
      }
      const resumed = await resume;
      if (resumed.error) throw resumed.error;
      if (!this._isCurrent(session)) { await this._cleanup(session); return; }
      if (continuous) {
        session.output = new PcmAudioOutput(session.context);
        session.playback = new PcmAudioPlayback(() => this._fetchChunk(session), {
          output: session.output, isCurrent: () => this._isCurrent(session),
          onError: (error) => this._stopSession(session, `error: ${this._errorMessage(error)}`),
        });
        session.playback.start(session.identity);
      }
      if (!capture) {
        this._setStatus("listening", this._message(config.mode === "duplex" ? "listenOnlyHttps" : "listenOnly"));
        return;
      }
      const granted = await permission;
      if (!this._isCurrent(session)) { await this._cleanup(session); return; }
      if (granted.error) {
        if (continuous && granted.error.name === "NotAllowedError") {
          this._setStatus("listening", this._message("listenOnlyDenied")); return;
        }
        throw granted.error;
      }
      const loaded = await worklet;
      if (!this._isCurrent(session)) { await this._cleanup(session); return; }
      if (loaded.error) throw loaded.error;
      const startMessage = { type: "doorfast/audio/start", config_entry_id: config.config_entry_id };
      if (config.station_id) startMessage.station_id = config.station_id;
      if (config.generation !== undefined) startMessage.generation = config.generation;
      if (session.listenerId) startMessage.listener_id = session.listenerId;
      const opened = await session.callWS(startMessage);
      session.captureId = opened.capture_id;
      if (!this._isCurrent(session)) { await this._cleanup(session); return; }
      if (typeof opened.capture_id !== "string" || !opened.capture_id) throw new Error("Invalid capture response");
      session.source = session.context.createMediaStreamSource(session.stream);
      const node = new AudioWorkletNode(session.context, "doorfast-pcm-processor", { numberOfInputs: 1, numberOfOutputs: 0, channelCountMode: "max" });
      session.worklet = node;
      session.sender = new PcmBatchSender(session.callWS, config.config_entry_id, session.captureId, {
        maxQueuedFrames: 5,
        stationId: config.station_id,
        generation: config.generation,
        onError: () => this._stopSession(session, `error: ${this._message("lost")}`),
      });
      node.port.onmessage = ({ data }) => {
        if (!this._isCurrent(session)) return;
        if (data?.type === "frame") session.sender.enqueue([data.frame]);
        else if (data?.type === "error") this._stopSession(session, `error: ${this._message("lost")}`);
      };
      session.source.connect(node);
      this._setStatus("active");
      this._audioPlayback?.start?.({ station_id: config.station_id, generation: config.generation });
    } catch (error) {
      await this._stopSession(session, `error: ${this._errorMessage(error)}`);
    }
  }

  _toggle() {
    return ["starting", "active", "listening"].includes(this._status) ? this._stop() : this._start();
  }

  _isCurrent(session) { return this._session === session && session.epoch === this._epoch; }

  async _fetchChunk(session) {
    const previous = this._readFlight;
    const request = (async () => {
      if (previous) await previous.catch(() => {});
      if (!this._isCurrent(session)) return { state: "empty" };
      const chunk = await session.callWS({ type: "doorfast/audio/listen_read", config_entry_id: session.config.config_entry_id, listener_id: session.listenerId });
      if (chunk?.state === "empty") return chunk;
      const safe = (value) => Number.isSafeInteger(value) && value >= 0;
      if (chunk?.state !== "chunk" || chunk.listener_id !== session.listenerId || chunk.generation !== session.identity.generation
          || !safe(chunk.generation) || !safe(chunk.previous_revision) || !safe(chunk.revision)
          || chunk.revision <= chunk.previous_revision || typeof chunk.discontinuity !== "boolean"
          || typeof chunk.pcm !== "string" || chunk.pcm.length > 4268
          || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(chunk.pcm)) throw new Error("Invalid audio response");
      const binary = atob(chunk.pcm);
      if (!binary.length || binary.length > 3200 || binary.length % 2 || btoa(binary) !== chunk.pcm) throw new Error("Invalid PCM response");
      return { ...chunk, pcm: Uint8Array.from(binary, (character) => character.charCodeAt(0)).buffer };
    })();
    this._readFlight = request;
    try { return await request; } finally { if (this._readFlight === request) this._readFlight = null; }
  }

  _releaseStream(session) {
    const stream = session.stream; session.stream = null;
    stream?.getTracks().forEach((track) => track.stop());
  }

  async _cleanup(session) {
    session.connection?.removeEventListener?.("disconnected", session.disconnected);
    session.playback?.stop(); session.playback = null;
    session.sender?.close(); session.sender = null;
    if (session.worklet) { session.worklet.port.onmessage = null; session.worklet.disconnect(); session.worklet = null; }
    session.source?.disconnect(); session.source = null;
    this._releaseStream(session);
    const closing = [];
    if (session.context && !session.contextClosed) {
      session.contextClosed = true;
      closing.push(Promise.resolve().then(() => session.context.close()).catch(() => {}));
    }
    for (const [field, type, key] of [["captureId", "stop", "capture_id"], ["listenerId", "listen_stop", "listener_id"]]) {
      const id = session[field]; session[field] = null;
      if (typeof id === "string" && id) closing.push(Promise.resolve().then(() => session.callWS({ type: `doorfast/audio/${type}`, config_entry_id: session.config.config_entry_id, [key]: id })).catch(() => {}));
    }
    await Promise.all(closing);
  }

  _stopSession(session, status = "idle") {
    if (this._isCurrent(session)) return this._stop(status);
    return this._cleanup(session);
  }

  _message(key) { return audioMessage(this._hass?.locale?.language ?? this._hass?.language, key); }

  _errorMessage(error) {
    const key = ({ status_not_ready: "noCall", call_not_established: "noCall", call_not_talking: "noCall", audio_call_not_established: "noCall",
      station_mismatch: "station", identity_mismatch: "station", audio_identity_mismatch: "identity",
      producer_busy: "busy", unsupported_audio_stream: "update" })[error?.code] ?? "lost";
    return this._message(key);
  }

  async _stop(finalStatus = "idle") {
    ++this._epoch;
    const session = this._session; this._session = null;
    this._audioPlayback?.stop?.();
    this._setStatus(finalStatus);
    if (session) await this._cleanup(session);
  }

  _setStatus(status, feedback) {
    this._status = status;
    const button = this.shadowRoot?.querySelector("button");
    const label = this.shadowRoot?.querySelector(".status");
    if (!button || !label) { this._render(); return; }
    const active = ["active", "starting", "listening"].includes(status);
    button.setAttribute("aria-pressed", String(active));
    button.textContent = this._config?.mode && this._config.mode !== "ptt"
      ? this._message(this._config.mode === "listen" ? (active ? "stopListen" : "listen") : (active ? "stopIntercom" : "intercom"))
      : (active ? "Release to stop" : "Hold to talk");
    button.setAttribute("aria-label", button.textContent);
    if (feedback !== undefined) this._feedback = feedback;
    else if (status !== "listening") this._feedback = null;
    label.textContent = this._feedback ?? status;
  }
}

if (!customElements.get("doorfast-ptt-card")) customElements.define("doorfast-ptt-card", DoorfastPttCard);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "doorfast-ptt-card")) {
  window.customCards.push({ type: "doorfast-ptt-card", name: "Doorfast Push to Talk", description: "Hold to send microphone audio to an active Doorfast call" });
}
