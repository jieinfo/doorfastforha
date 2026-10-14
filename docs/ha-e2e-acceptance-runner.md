# Doorfast HA/VM acceptance runner

Continuous audio in this round is limited to **already-established/incoming
`talking` calls**. The formal outbound CALL path has an existing call-control
wiring defect; outbound duplex acceptance is pending a separate repair. Do not
make the card automatically establish a call as a workaround. Stopping audio
releases the listener/producer and does not hang up or unlock the station.

Task 6 runs only local synthetic tests and host builds. No real HA runner,
installation, dashboard edit, device control, TLS change, publication or SDK
build was performed. The commands below that connect to HA are procedures for
a separately authorized acceptance session, not evidence from this task.
HTTP listening is limited to a trusted LAN; HTTPS is recommended for
remote/public voice. Never expose the Doorfast CGI directly to the Internet.

This repository contains a test-only, dependency-free acceptance runner for the
Doorfast microphone bridge. It targets the Home Assistant WebSocket API and the
Doorfast HTTP bridge implemented by Doorfast. The runner keeps PCM bodies and
all producer credentials in memory, and writes only redacted JSONL evidence.

Session diagnostics require an updated Doorfast producer and its optional
diagnostics extension for complete classification. Older bridges or unavailable
extensions safely show `unknown` codes and `None` (JSON `null`) numbers/terminal
identity. The local session tests below are synthetic regression evidence;
Actions, installed diagnostic inspection, native frontend localization and
field checks remain pending. The audio runner does not establish those results.

The existing runner records these historical source references in its evidence:

* `doorfastforha` `0bd2474` (HA main after setup rollback safety)
* `doorfast` `a5fcca3` (Doorfast main after the HTTP PCM contract PR)

Those embedded references are not the continuous-audio branch revisions. The
Task 6 report records the exact HA audio commit and Doorfast
`b632b8fc0add0e191d5a31c537c30a0fe184b94e` used for the new local contract checks.
The runner covers its existing uplink lifecycle; it does not prove the new
v1 listening contract or browser output by itself.

The loopback fixture is the default mode. It starts a private HTTP server on
`127.0.0.1`, with two independent synthetic Doorfast hosts, and supplies a
talking call with deterministic runtime IDs and generations. This is a protocol
and lifecycle test; it does not claim physical audio delivery. The fixture is
never used implicitly in installed mode.

## Distinguish the evidence levels

| Level | What it proves | What it cannot prove |
| --- | --- | --- |
| Local Python composition | Real `AudioIdentity`, HTTP/WAV parsing, hub, manager and producer with synthetic streamed HTTP; independent listeners, resync and same-generation reboot isolation; cleanup | Installed HA operation, browser microphone or physical audio |
| Local Node composition | Real card, PCM decoder, playback, native output and sender against controlled DOM/Web Audio/WS boundaries; scheduled nodes, late-result isolation and stop cleanup | Browser/device audio hardware, audible sound or echo cancellation |
| Runner fixture against authorized HA | Real HA API/config-flow/WS ownership and existing uplink cleanup, with synthetic endpoints | New downlink/native output acceptance or physical delivery |
| Browser at localhost | Browser interaction/microphone APIs on a secure-context exception | HTTPS deployment, network trust or real device audio |
| Browser through authorized HTTPS | Real browser permission, static resource loading and bidirectional session setup | Audible two-way delivery until a physical observer checks it |
| Field speaker/microphone check | Already-talking incoming call heard in both directions, echo and audible delay observed with named devices/browser | Outbound CALL acceptance or untested SDK/target builds |

Synthetic samples are not recordings. Evidence should contain only check
results, request counts, lengths and digests; do not save PCM/WAV, producer
tokens, HA tokens or raw sensitive responses. The 300 ms bound is local
scheduled buffering/cache freshness. Source timestamps measure selected versus
latest bridge publication, not capture/end-to-end delay.

## Check session diagnostics without controls

Use the existing controller status and station Preview status sensors, with
the [attribute reference in README](../README.md#read-session-diagnostics).
This inspection adds no buttons/entities, changes no dashboard and invokes no
monitor, call, hangup or unlock service.

1. Before a deployment update, require passing Actions for the intended package;
   then update through HACS as a separate deployment step. Record producer,
   integration and HA/frontend versions. Local suites alone do not establish CI
   or installation acceptance.
2. Inspect refreshed status and the existing sensor attributes for each of the
   three configured stations. Use synthetic fixture IDs `gate_main`, `gate_side`
   and `gate_b2` only in local tests; compare actual configured station IDs and
   generations during an authorized installation inspection. Retain only safe
   diagnostic codes and numbers, never complete raw status or access materials.
3. Compare current `media.sessions[]` diagnostics with that station's attributes.
   `media.last_results[]` contains only its latest terminal result. A current
   generation 9 with `monitor_end_reason=none` must retain its current reason even
   if terminal generation 8 ended with `local_hangup`; independently,
   `monitor_last_generation` may be 8. With no current session, a unique valid
   terminal record can supply the reason and `none/0` timer. Invalid or ambiguous
   current data stays unknown rather than borrowing history. New runtime empty
   history clears terminal markers.
4. Check timer meaning against the same sampled bridge snapshot. Relative
   `remaining_ms` is sampled by Doorfast, never extrapolated in the frontend.
   Confirmation expiry is `call_fault=confirmation_timeout` and can leave
   `call_end_reason=none`; whole-session expiry is `end_reason=session_timeout`.
   `heartbeat_timeout` belongs only to call diagnostics. Recovering media can
   report `source_stalled` or `capacity_busy` while still current. Accepted
   `publisher_failed`, `confirmation_rejected` and `runtime_reset` vocabulary
   does not establish an implemented producer trigger. Insufficient evidence is
   `unknown`; runtime replacement does not manufacture a reset terminal result.
5. Compare accepted event refresh and the next poll for the same station and
   generation. The local fixtures cover equivalence plus stale generation,
   revision/runtime and conflicting embedded event data rejection. Event loss
   must not restore an earlier terminal reason over a current session. Confirm
   old bridge missing fields yield unknown codes and `None` numbers, not zero or
   fabricated success/failure.
6. In a later authorized frontend/field acceptance, inspect English and Simplified
   Chinese native attribute labels on the existing sensors; older frontends may
   show stable codes. Compare the fields with Doorfast logs from separately
   authorized first-frame timeout/short-stream recovery, real call end and normal
   stop cases at stations 1, 2 and B2. Read-only inspection does not initiate
   those cases, and diagnostic results do not prove audible sound, real video
   delivery or physical full-duplex operation.

The controller's `call_last_station_id` / `call_last_generation` pair attributes
the last terminated call independently of the current raw `call`. Diagnostic
properties are bounded codes, numbers and a validated station ID; they add no
tokens, passwords, SDP, media bodies, network addresses or access materials.

## Historical local setup for an authorized runner session

The earlier runner documentation recorded this setup; it was not revalidated
by Task 6:

* Docker image `ghcr.io/home-assistant/home-assistant:2024.11.0`
* HA HTTP port `127.0.0.1:18124` mapped to container port `8123`
* configuration directory `/Users/shenwenjie/Documents/PVE/.doorfast-ha-audit.NrqniK`
* ImmortalWrt VM SSH `127.0.0.1:2222`, LuCI `http://127.0.0.1:8080/`
* VM test UDP forward `127.0.0.1:18300` to guest port `8300`

The HA container must have the `doorfast` custom component from
`doorfastforha` `0bd2474` installed and be running. A long-lived administrator
token can be supplied in a file, or the runner can read the administrator
refresh token from HA's `.storage/auth` file without printing it.

## Fixture run against real HA (requires separate authorization)

This command creates two temporary Doorfast config entries through HA's real
config-flow REST API, verifies the four bundled frontend resources,
then exercises the existing uplink start, bounded PCM submit, stop, WebSocket disconnect cleanup,
multi-entry isolation, hangup cleanup, generation cleanup, unload cleanup, and
reload. It deletes only the entries created by this run during cleanup.

```sh
python3 run_acceptance.py \
  --mode fixture \
  --ha-url http://127.0.0.1:18124 \
  --ha-auth-store /Users/shenwenjie/Documents/PVE/.doorfast-ha-audit.NrqniK/.storage/auth \
  --evidence artifacts/fixture.jsonl
```

When HA runs in Docker or Colima and cannot route to the host loopback, bind
the fixture to an isolated host interface and advertise that reachable host
address to HA. For example, on the validated local setup:

```sh
  --fixture-bind-host 0.0.0.0 --fixture-advertise-host 10.10.1.15
```

Native HA processes can use the default loopback values. Do not expose the
fixture beyond the isolated test network.

The run fails if HA is unavailable, the custom integration is missing, or any
WebSocket lifecycle invariant is not observed. It never waits on GitHub
Actions. Inspect the JSONL file for check-level evidence; `token`, `pcm`,
`audio_session`, and `body` fields are always redacted, while opaque capture
IDs are represented only by a SHA-256 fingerprint and length.

## Installed endpoint mode (requires separate authorization)

Installed mode is explicit and requires a configured HA entry. It talks to the
endpoint already configured in that entry and never starts the private fixture
or creates a config entry:

```sh
python3 run_acceptance.py \
  --mode installed \
  --ha-url http://127.0.0.1:18124 \
  --ha-token-file /secure/path/ha-token \
  --entry-id YOUR_ENTRY_ID \
  --second-entry-id OPTIONAL_SECOND_ENTRY_ID \
  --evidence artifacts/installed.jsonl
```

The installed endpoint must already expose a talking call with active audio
transmit state. The runner can verify static resources and all cleanup paths
that do not require changing the endpoint. Generation mutation is reported as
skipped unless an endpoint-specific test control is added in a future harness;
the runner does not invent a production control URL. Multi-entry isolation is
reported as skipped unless `--second-entry-id` is provided.

No production claim should be made from fixture evidence. Physical MT8157
acceptance remains a separate stage after a real talking call and a verified
PCM capture or playback observer are available.

For a later authorized browser/field check, select your real HA entry and
station (do not paste the synthetic `one` / `gate_main` values):

```yaml
type: custom:doorfast-ptt-card
config_entry_id: one
station_id: gate_main
mode: duplex
name: Main gate intercom
```

Confirm the selected incoming call is already `talking`; record the actual
runtime/generation and browser/device versions without credentials. With HTTPS
and microphone permission, click once and listen in both directions. Repeat
with `mode: listen`, HTTP trusted-LAN access, and denied microphone permission:
these must receive audio without opening a microphone producer. Click again,
hide/blur the page, remove the card or disconnect HA and verify local audio
stops. Reconnect must not automatically reacquire the microphone. Permission
changes need an explicit new start. Record speaker audibility, echo and
observed delay separately from fixture results. Outbound duplex and OpenWrt
SDK/`DF_WITH_UBUS` compilation remain pending separate work.

## v1 bridge contract checked by the local fixtures

`GET /api/v1/audio/latest.wav` supplies `runtime` (16 lowercase hex),
`station_id` and positive `generation`, with optional `after` revision. A
successful chunk carries `X-Doorfast-Runtime-Id`, `X-Doorfast-Station-Id`,
`X-Doorfast-Generation`, `X-Doorfast-Audio-Previous-Revision`,
`X-Doorfast-Audio-Revision`, `X-Doorfast-Audio-Dropped-Bytes`,
`X-Doorfast-Audio-Discontinuity` (`0`/`1`),
`X-Doorfast-Audio-Timestamp-Ms`, `X-Doorfast-Audio-Latest-Timestamp-Ms`,
`X-Doorfast-Audio-Format: s16le`, `X-Doorfast-Audio-Sample-Rate: 8000`,
and `X-Doorfast-Audio-Channels: 1`.

| HTTP response | Behavior |
| --- | --- |
| `200` WAV | Parse bounded mono PCM; advance only each consuming listener's cursor |
| `304` with matching identity headers | Valid empty result; never replay PCM |
| `404`, `code: audio_unavailable`, matching identity headers | Valid empty result |
| `503`, `code: audio_temporarily_unavailable` | Retry without advancing the cursor; five seconds of continuous invalid results releases ownership |
| `409`, `code: audio_cursor_expired` | Reset the bridge cursor and resync within the same identity, marking discontinuity |
| `409`, `code: audio_identity_mismatch`, or mismatched chunk headers | Reject and release the shared hub and linked microphone |
| `400`, `code: invalid_request` | Reject invalid identity/cursor parameters |

Canonical WAV is at most 3244 bytes with at most 3200 PCM bytes. The HA reader
rejects at 4096 consumed bytes without a byte-4097 EOF probe, so all accepted
bodies are below 4096; arbitrary extended WAVs at that boundary are not
guaranteed. Four listeners share at most four cached chunks and one read.
Producer session credentials remain backend-only; the browser gets opaque
listener/capture IDs. Existing legacy PTT and bridge requests remain covered by
the full regression suites.

## Local checks

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q custom_components/doorfast
node --test tests/js/*.mjs
python3 -m json.tool custom_components/doorfast/translations/en.json >/dev/null
python3 -m json.tool custom_components/doorfast/translations/zh-Hans.json >/dev/null
git diff --check
python3 -m compileall -q doorfast_ha_e2e run_acceptance.py
```

Run the focused composition checks with:

```sh
python3 -m unittest discover -s tests -p test_audio_contract.py
node --test tests/js/test_duplex_lifecycle.mjs
```

The Task 6 report and raw logs live in
`.superpowers/sdd/2026-10-12-ha-continuous-duplex-audio/`. Local host checks do
not establish OpenWrt SDK or `DF_WITH_UBUS` target-build evidence.

Session normalization and real entity/event/poll fixture coverage live in
`tests/test_session_diagnostics.py` and `tests/test_station_status_entities.py`.
They cover legacy bridge fields, three-station isolation, current generation 9
versus terminal 8, stable sensor IDs/options/counts, safe properties and native
translation dictionaries. They do not render a real HA frontend or perform an
installed/field diagnostic session. Full-suite failure-path fixture logs are
expected; assess the test summary and command exit status.
