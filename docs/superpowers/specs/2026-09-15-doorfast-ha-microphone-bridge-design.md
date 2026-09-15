# Doorfast Home Assistant Microphone Bridge Design

## Goal

Add push-to-talk microphone audio from an authenticated Home Assistant browser to Doorfast without exposing the Doorfast producer token or confusing audio from different daemon runtimes and calls. The transport carries 8 kHz mono signed 16-bit little-endian PCM in exact 320-byte frames. Physical MT8157 playback remains a later hardware acceptance claim.

## Four slices

1. **Backend producer primitives (this change).** A pure Python state machine validates a fresh Doorfast status, owns the private producer token, submits bounded PCM batches with one request in flight, and recovers safely from ambiguous replies. `DoorfastClient` gains only the binary HTTP primitives needed by that state machine.
2. **Authenticated Home Assistant control.** Later WebSocket commands will start, submit, and stop capture per config entry. Registration is global and idempotent; ownership is released on browser disconnect, unload, or hangup. The browser receives an opaque capture identifier, never the Doorfast token.
3. **Push-to-talk browser card.** A bundled Lovelace card and AudioWorklet will request the microphone from a user gesture in a secure context, low-pass resample variable-rate Float32 input to 8 kHz mono s16le, form exact frames, batch at most five, and use one in-flight request with bounded drop/backpressure behavior.
4. **System acceptance.** A real Home Assistant instance and Doorfast ImmortalWrt VM will verify reload, disconnect, multi-entry ownership, and secure-context behavior. Downlink playback and physical MT8157 intelligibility, latency, and echo testing follow separately.

## Backend contract

The producer lifecycle is `IDLE -> OPENING -> ACTIVE -> RECOVERING -> ACTIVE/IDLE` and `ACTIVE/RECOVERING -> STOPPING -> IDLE`. Start always refreshes status and admits only an exact 16-character lowercase hexadecimal runtime, `call.session == talking`, positive integer call generation, `audio_tx.active is true`, and an equal audio transmit generation.

Session open posts an empty body to `/api/v1/audio/session` with `runtime` and `generation` query values. Submit posts one to five consecutive 320-byte frames to `/api/v1/audio/submit.pcm`, additionally sending `sequence`, `Content-Type: application/octet-stream`, and the private token in `X-Doorfast-Audio-Session`. End posts an empty body to `/api/v1/audio/session/end` with the token header. The token remains encapsulated by the producer and is never returned through a public property or log.

Every JSON response is validated as an object with the expected success or error shape, the exact runtime and generation, bounded integer fields, and an exact lowercase 32-hex session token where applicable. HTTP 409 and 503 bodies are parsed before the status is interpreted so authoritative `accepted_frames` and `next_sequence` are available for recovery.

## Recovery rules

At most one recovery pass performs one fresh status read and, when the lease is known lost, one session-open attempt. There is no retry loop. Runtime or generation change stops the producer and clears all private state. `producer_busy` ends local ownership. `session_expired` opens a new session once. A valid partial 503 advances to the reported server cursor after refreshing status. `state_unavailable` after any accepted prefix is treated as a non-durable cursor: the response body is discarded and the next new batch probes the reported cursor; a subsequent gap or duplicate response reconciles to the server cursor.

A transport failure or lost response is ambiguous. The failed body is forgotten. The old token and local cursor are retained only long enough for a fresh same-identity status check; the next newly captured body probes that cursor. Duplicate or gap replies update the cursor and discard that new body. The client never resends bytes from a failed or ambiguous request and never builds an unbounded audio queue.

Stop clears local identity and token even when the best-effort end request fails. All public operations are serialized with one `asyncio.Lock`, which ensures no more than one PCM HTTP request is in flight.

## Slice 1 scope boundary

This slice has no HA WebSocket registration, microphone permissions, JavaScript, service, media-player entity, raw PCM public API, or new dependency. It preserves the existing status, control, video, and downlink-audio behavior.

## Browser card contract

The bundled `doorfast-ptt-card` is registered as a versioned Home Assistant extra
module and served from the collision-resistant `/doorfast_static` path. Home
Assistant 2024.11 supports the asynchronous static-path API used here. The static
route remains installed across integration reloads, while an entry refcount adds
the module for the first loaded entry and removes it after the last unload. A
dashboard that was already open during first registration needs one page reload.

The worklet uses its actual browser `sampleRate`, averages every input channel,
applies a 127-tap Blackman-windowed FIR low-pass filter, and performs streaming area resampling.
It emits only 160-sample, 320-byte little-endian frames. The sender permits one
WebSocket request in flight plus five queued frames. When that queue fills it
drops the oldest unsent audio to bound latency; a failed or ambiguous batch is
discarded and never replayed. A lifecycle epoch prevents a late response from
resuming transmission after stop.

Capture begins only from pointer or keyboard activation in a secure context. The
card creates and resumes its AudioContext during that activation. Release,
pointer cancellation, lost pointer capture, blur, page hiding, HA WebSocket
disconnect, track end, card removal, configuration change, or processing and
transport errors stop local media immediately and request best-effort backend
release using the entry identity stored with that capture.
