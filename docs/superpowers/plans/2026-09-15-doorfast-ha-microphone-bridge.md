# Doorfast Home Assistant Microphone Bridge Implementation Plan

## Slice 1: backend producer primitives (this PR)

1. Add fake-HTTP unit tests for exact open, submit, and end requests plus JSON parsing on 409/503.
2. Add producer tests for strict status/response validation, successful lifecycle, one-request serialization, partial acceptance, lost responses, non-durable `state_unavailable`, duplicate/gap cursor reconciliation, generation changes, busy/expiry recovery, and best-effort stop.
3. Implement minimal binary JSON methods on `DoorfastClient` without changing existing command/media behavior.
4. Implement the dependency-free PCM producer and explicit lifecycle in `custom_components/doorfast/pcm.py`.
5. Run the complete unittest suite and `compileall`; review the branch diff for token exposure and scope creep.

## Slice 2: authenticated HA WebSocket ownership

Register global commands once; map opaque capture IDs to one producer per config entry; enforce authenticated administrative start/submit/stop; release ownership on disconnect, unload, hangup, and generation change. Test multi-entry isolation and registration/reload behavior.

## Slice 3: browser push-to-talk card

Add a bundled card and AudioWorklet with secure-context/user-gesture capture, low-pass streaming resampling to exact 8 kHz mono s16le frames, base64 batches no larger than 1600 bytes, one request in flight, and bounded dropping under backpressure. Verify DSP and framing with Node tests.

## Slice 4: integrated acceptance

Run a real HA instance against the Doorfast ImmortalWrt VM. Verify start/stream/stop, reload, disconnect, hangup, multi-entry isolation, token privacy, and HTTPS requirements. Record that VM ingress evidence does not establish physical loudspeaker compatibility, then perform separate MT8157 hardware audio tests.
