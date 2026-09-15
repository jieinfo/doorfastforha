import { PcmBatchSender } from "./audio-stream.mjs?v=0.1.0";

const WORKLET_URL = "/doorfast_static/doorfast-pcm-worklet.mjs?v=0.1.0";

export class DoorfastPttCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._status = "idle";
    this._epoch = 0;
    this._onVisibility = () => { if (document.hidden) this._stop(); };
    this._onBlur = () => this._stop();
    this._onDisconnected = () => this._stop();
  }

  setConfig(config) {
    if (!config?.config_entry_id || typeof config.config_entry_id !== "string") {
      throw new Error("Doorfast PTT requires config_entry_id");
    }
    const next = { config_entry_id: config.config_entry_id, name: config.name ?? "Doorfast Push to Talk" };
    if (this._config?.config_entry_id === next.config_entry_id && this._config?.name === next.name) return;
    if (this._config) this._stop();
    this._config = next;
    this._render();
  }

  set hass(value) {
    if (this._hass?.connection !== value?.connection) {
      this._hass?.connection?.removeEventListener?.("disconnected", this._onDisconnected);
      value?.connection?.addEventListener?.("disconnected", this._onDisconnected);
    }
    this._hass = value;
  }

  getCardSize() { return 2; }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    window.addEventListener("blur", this._onBlur);
    this._hass?.connection?.addEventListener?.("disconnected", this._onDisconnected);
    this._render();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    window.removeEventListener("blur", this._onBlur);
    this._hass?.connection?.removeEventListener?.("disconnected", this._onDisconnected);
    this._stop();
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const active = this._status === "active" || this._status === "starting";
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
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      this._setStatus("error: HTTPS or localhost is required"); return;
    }
    const epoch = ++this._epoch;
    const entryId = this._config.config_entry_id;
    this._setStatus("starting");
    let stream;
    let captureId;
    let context;
    try {
      context = new AudioContext({ latencyHint: "interactive" });
      this._audioContext = context;
      const streamPromise = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      const workletPromise = context.audioWorklet.addModule(WORKLET_URL);
      const resumePromise = context.resume();
      // Always settle the microphone request before handling worklet failure so
      // a late permission grant cannot leave an unowned live track behind.
      stream = await streamPromise;
      await Promise.all([workletPromise, resumePromise]);
      if (epoch !== this._epoch) { stream.getTracks().forEach((track) => track.stop()); return; }
      this._stream = stream;
      for (const track of stream.getTracks()) track.addEventListener("ended", () => this._stop(), { once: true });
      const opened = await this._hass.callWS({ type: "doorfast/audio/start", config_entry_id: entryId });
      captureId = opened.capture_id;
      if (epoch !== this._epoch) {
        await this._bestEffortStop(captureId, entryId); return;
      }
      this._captureId = captureId;
      this._sessionEntryId = entryId;
      const source = context.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(context, "doorfast-pcm-processor", { numberOfInputs: 1, numberOfOutputs: 0, channelCountMode: "max" });
      this._source = source; this._worklet = node;
      this._sender = new PcmBatchSender(this._hass.callWS.bind(this._hass), entryId, captureId, {
        maxQueuedFrames: 5,
        onError: () => this._stop("error: audio connection lost"),
      });
      node.port.onmessage = ({ data }) => {
        if (epoch !== this._epoch) return;
        if (data?.type === "frame") this._sender.enqueue([data.frame]);
        else if (data?.type === "error") this._stop("error: audio processing failed");
      };
      source.connect(node);
      this._setStatus("active");
    } catch (error) {
      if (captureId) await this._bestEffortStop(captureId, entryId);
      stream?.getTracks().forEach((track) => track.stop());
      if (context && context.state !== "closed") await context.close().catch(() => {});
      if (epoch === this._epoch) this._setStatus(`error: ${error instanceof Error ? error.message : "unable to start"}`);
    }
  }

  async _bestEffortStop(captureId, entryId) {
    try {
      await this._hass?.callWS({ type: "doorfast/audio/stop", config_entry_id: entryId, capture_id: captureId });
    } catch (_) { /* Backend also releases ownership when this connection closes. */ }
  }

  async _stop(finalStatus = "idle") {
    const stopEpoch = ++this._epoch;
    const captureId = this._captureId;
    const entryId = this._sessionEntryId;
    this._captureId = null;
    this._sessionEntryId = null;
    this._sender?.close(); this._sender = null;
    this._worklet?.disconnect(); this._worklet = null;
    this._source?.disconnect(); this._source = null;
    this._stream?.getTracks().forEach((track) => track.stop()); this._stream = null;
    const context = this._audioContext; this._audioContext = null;
    if (context && context.state !== "closed") await context.close().catch(() => {});
    if (captureId && entryId) await this._bestEffortStop(captureId, entryId);
    if (stopEpoch === this._epoch) this._setStatus(finalStatus);
  }

  _setStatus(status) {
    this._status = status;
    const button = this.shadowRoot?.querySelector("button");
    const label = this.shadowRoot?.querySelector(".status");
    if (!button || !label) { this._render(); return; }
    const active = status === "active" || status === "starting";
    button.setAttribute("aria-pressed", String(active));
    button.textContent = active ? "Release to stop" : "Hold to talk";
    label.textContent = status;
  }
}

if (!customElements.get("doorfast-ptt-card")) customElements.define("doorfast-ptt-card", DoorfastPttCard);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "doorfast-ptt-card")) {
  window.customCards.push({ type: "doorfast-ptt-card", name: "Doorfast Push to Talk", description: "Hold to send microphone audio to an active Doorfast call" });
}
