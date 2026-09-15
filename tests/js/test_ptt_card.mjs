import assert from "node:assert/strict";
import test from "node:test";

globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = { innerHTML: "", querySelector: () => null }; }
};
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

test("configuration changes stop an old capture with its stored entry identity", async () => {
  const card = new DoorfastPttCard();
  const calls = [];
  card._render = () => {};
  card._hass = { callWS: async (message) => { calls.push(message); } };
  card._config = { config_entry_id: "old", name: "Old" };
  card._captureId = "opaque";
  card._sessionEntryId = "old";
  card.setConfig({ config_entry_id: "new" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, [{ type: "doorfast/audio/stop", config_entry_id: "old", capture_id: "opaque" }]);
});
