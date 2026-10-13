import assert from "node:assert/strict";
import test from "node:test";
import { audioMessage } from "../../custom_components/doorfast/frontend/audio-messages.mjs";

test("audio feedback translates safe user-facing keys with English fallback", () => {
  assert.equal(audioMessage("zh-Hans", "micDenied"), "麦克风权限被拒绝");
  assert.equal(audioMessage("zh-CN", "listen"), "收听");
  assert.equal(audioMessage("fr", "lost"), "Audio connection lost");
  assert.equal(audioMessage("en", "unknown backend secret"), "Audio connection lost");
  for (const key of ["listen", "stopListen", "intercom", "stopIntercom", "listenOnly", "https", "micDenied", "noCall", "station", "busy", "update", "lost"]) {
    assert.ok(audioMessage("en", key)); assert.ok(audioMessage("zh-CN", key));
  }
});
