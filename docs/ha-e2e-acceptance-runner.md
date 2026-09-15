# Doorfast HA/VM acceptance runner

This repository contains a test-only, dependency-free acceptance runner for the
Doorfast microphone bridge. It targets the Home Assistant WebSocket API and the
Doorfast HTTP bridge implemented by Doorfast. The runner keeps PCM bodies and
all producer credentials in memory, and writes only redacted JSONL evidence.

The source references under test are recorded in every evidence file:

* `doorfastforha` `819f94b` (HA main after the PTT card PR)
* `doorfast` `a5fcca3` (Doorfast main after the HTTP PCM contract PR)

The loopback fixture is the default mode. It starts a private HTTP server on
`127.0.0.1`, with two independent synthetic Doorfast hosts, and supplies a
talking call with deterministic runtime IDs and generations. This is a protocol
and lifecycle test; it does not claim physical audio delivery. The fixture is
never used implicitly in installed mode.

## Existing local setup

The validated local HA setup is:

* Docker image `ghcr.io/home-assistant/home-assistant:2024.11.0`
* HA HTTP port `127.0.0.1:18124` mapped to container port `8123`
* configuration directory `/Users/shenwenjie/Documents/PVE/.doorfast-ha-audit.NrqniK`
* ImmortalWrt VM SSH `127.0.0.1:2222`, LuCI `http://127.0.0.1:8080/`
* VM test UDP forward `127.0.0.1:18300` to guest port `8300`

The HA container must have the `doorfast` custom component from
`doorfastforha` `819f94b` installed and be running. A long-lived administrator
token can be supplied in a file, or the runner can read the administrator
refresh token from HA's `.storage/auth` file without printing it.

## Fixture run against real HA

This command creates two temporary Doorfast config entries through HA's real
config-flow REST API, verifies the four bundled frontend resources,
then exercises start, bounded PCM submit, stop, WebSocket disconnect cleanup,
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

## Installed endpoint mode

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

## Local checks

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q doorfast_ha_e2e run_acceptance.py
```
