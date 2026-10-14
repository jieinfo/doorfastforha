# Doorfast for Home Assistant

This integration is a clean Home Assistant adapter for Doorfast host mode. It covers door unlock, elevator up/down calls, incoming-call notifications, hangup and optional video.

Continuous audio attaches only to an **already-established/incoming `talking`
call** at the selected station. This round has synthetic contract evidence;
deployment, HTTPS microphone use, physical listening/echo checks and OpenWrt SDK
validation remain separate, unperformed stages. Outbound duplex is **not
accepted**: the existing formal outgoing CALL wiring still needs a separate
repair. The audio card does not automatically call or answer a station.
HTTP listening is for a trusted LAN only; use HTTPS for remote/public voice
access. Do not expose the Doorfast CGI directly to the public Internet.

Release 0.3.0 adds dynamic multi-station discovery, station reachability, and
per-station monitor cameras. Configure one go2rtc stream for each station;
repository fixture acceptance is simulated and does not prove physical-device
concurrency.

The integration talks to the Doorfast HTTP bridge at `http://<host>/cgi-bin/doorfast`. The bridge maps requests to the local `ubus` object and exposes `GET /api/v1/status`, `POST /api/v1/unlock`, `/api/v1/answer`, `/api/v1/hangup` and `/api/v1/call_elevator`. Enter the host URL in the setup form; the CGI path is added automatically.

Doorfast status is authoritative for command acceptance, while physical door/elevator confirmation remains a separate status field and is shown as returned by the bridge. Answer and hangup remain bound to the current call generation and runtime. Unlock requests instead carry only the configured `station_id`, so each discovered station has its own lock entity and can be unlocked without an active call. A daemon restart still changes `runtime_id`; cached media state and call-event high-water marks are then discarded so a delayed media or call operation cannot collide with a reused generation.

Each station lock entity sends a momentary unlock command and remains shown as locked because Doorfast does not yet receive a physical door-position signal. The legacy controller-level lock is retained only for an active incoming call and is unavailable when no station is ringing. Its attributes expose the protocol state, generation, raw reply status and `physical_result_confirmed` value reported by the bridge.

Each outdoor-station device also has **Preview status** and **Unlock progress / latest reply** sensors, with English and Simplified Chinese labels. Preview distinguishes requesting, waiting for the first frame, live source, reconnecting with the last frame, in-call, stopping, preempted and failed. A running publisher (`ready=true`) alone never means live video: the bridge must also report `source_live=true` for that exact station. Missing legacy fields show unknown. Bridge connection failure updates availability instead of leaving an old live status visible. Status feedback is read-only and uses the existing event/poll refresh cadence; it does not alter preview leases, request frequency or cooldown.

The camera attributes include `monitor_phase`, `monitor_source_live`, request/busy counters and `monitor_first_frame_latency_ms` when valid. This latency measures **one Doorfast attempt**, not total time from a user's click; retries reset the attempt clock. Only known numeric fields are exposed, never raw media dictionaries or credentials. These sensors use the [Home Assistant enum sensor contract](https://developers.home-assistant.io/docs/core/entity/sensor/).

Unlock progress uses `access.manual_station_id` for media preparation and `access.station_id` for the latest protocol transaction. Old bridges without result attribution cannot display a station-specific completion/rejection. During preparation, a previous transaction's generation/reply is not shown as the current request. After preparation ends without sending a request, there is no new protocol result; the sensor can return to idle or the last attributed reply. Only the latest global protocol transaction is available from Doorfast, so these sensors are not a per-station history. “Station acknowledged” is **not** physical door-open confirmation. Updating Doorfast as well as this integration enables complete reply attribution; existing control entity IDs are unchanged.

The camera requests snapshots for the current call generation and reuses a
cached frame only when the Doorfast bridge returns `304 Not Modified` or a
short-lived `503 Service Unavailable`. A call-generation change, unavailable
video status, or `404`/`409` response clears the cached image so a previous
visitor cannot appear in a later call.

The continuous audio card plays bounded PCM from Doorfast v1 WAV chunks through
the browser's Web Audio output. The backend binds listeners to runtime, station
and generation; a restart with a reused generation still invalidates the old
session. Each listener consumes its own cursor, while up to four listeners share
one HTTP reader and a four-chunk cache. Unchanged chunks are not replayed;
temporary failures preserve the cursor, and an expired cursor resynchronizes
within the same identity. Legacy audio-client and PTT paths remain supported.

## Read session diagnostics

Update Doorfast as well as this integration to obtain classified diagnostics.
An old bridge, missing optional diagnostics module, unsupported enum or malformed
field produces `unknown` for diagnostic codes and `None` (JSON `null`) for
unavailable numbers or terminal identity. Unknown does not mean idle, success or
failure. Existing preview phases and control entity IDs remain unchanged.

Read these attributes on the existing controller status and per-station
**Preview status** sensors; no extra entity or button is created:

| Sensor | Attributes | Meaning |
| --- | --- | --- |
| Controller status | `call_timer`, `call_remaining_ms` | Current call timer: `none`, `confirmation` or `session`; sampled relative milliseconds |
| Controller status | `call_end_reason`, `call_fault` | Call end result and fault classification, kept distinct |
| Controller status | `call_last_station_id`, `call_last_generation` | Validated station/generation pair for the last terminated call, separate from the current raw `call` identity |
| Preview status | `monitor_timer`, `monitor_remaining_ms` | Selected station timer; also supports `first_frame` and `reconnect` |
| Preview status | `monitor_end_reason`, `monitor_failure_reason` | Current station diagnostics, or its latest terminal result when no current session exists |
| Preview status | `monitor_last_generation` | Generation of that station's latest valid terminal result, never a substitute for the active generation |

The producer's terminal call station value `unknown` is ambiguous, so HA sets
both `call_last_station_id` and `call_last_generation` to `None` for that value.
This also applies to a station actually named `unknown` until the producer
provides a distinguishable identity; raw call/audio and station control remain
unchanged.

`remaining_ms` is computed by Doorfast when sampling status. HA displays the
latest sample through the existing event/poll refresh cadence; it does not
extrapolate a frontend countdown or expose a monotonic deadline. Known no-timer
state is `none/0`. Persistent preview `requesting` has a repeated request
interval, not a total confirmation deadline, so it also reports `none/0`.

Confirmation expiry sets `call_fault=confirmation_timeout` while the call can
remain active with `call_end_reason=none`. `session_timeout` is a whole-session
deadline end reason, not the confirmation fault. `heartbeat_timeout` is a
call-only fault/end classification. Media `source_stalled` or `capacity_busy`
can describe a recovering current session without implying termination.
`publisher_failed`, `confirmation_rejected` and `runtime_reset` are accepted
vocabulary without verified producer triggers in this implementation; a label
alone does not prove publishing failure, explicit refusal or a reset result.
Insufficient producer evidence remains `unknown`, and a new runtime starts
with empty terminal history.

Current `media.sessions[]` and per-station `media.last_results[]` are independent.
For example, these synthetic Preview status attributes describe current
generation 9 waiting for a frame while the terminal marker still refers to
generation 8:

```json
{
  "generation": 9,
  "monitor_timer": "first_frame",
  "monitor_remaining_ms": 900,
  "monitor_end_reason": "none",
  "monitor_failure_reason": "none",
  "monitor_last_generation": 8
}
```

Generation 8's stopped-session reason cannot become generation 9's current
failure. Unsupported current collections and duplicate or invalid matching
current records cannot borrow historical reasons; another station's result
cannot fill this station's attributes. A supported empty current list can use
that station's valid terminal result.
Only whitelisted codes, validated numbers and the validated terminal call
station ID enter the new properties, without tokens, media bodies or addresses.

English and Simplified Chinese native attribute labels are supplied under the
existing sensor translation keys. Local tests validate their structure and
codes; actual HA frontend rendering is pending, and older frontends may show
stable codes. See the [read-only diagnostic acceptance procedure](docs/ha-e2e-acceptance-runner.md#check-session-diagnostics-without-controls)
for the remaining CI, deployment and field checks. Status diagnostics do not
establish browser listening or physical full-duplex acceptance.

## Active WebRTC preview

When Doorfast reports the media module as installed and available, the camera
entity exposes an opaque
`doorfast://<config_entry_id>/station/<station_id>/preview` source. Native camera
WebRTC hooks delegate signaling to the entry's `CameraWebRTCProvider` through
HA-local go2rtc while Doorfast publishes the H.264 preview to the go2rtc RTSP
ingress. Use the
`doorfast.start_monitor` and `doorfast.stop_monitor` services for explicit
monitor control; camera viewers also acquire and release a generation
automatically. These cameras support WebRTC only, not HLS, recording, or JPEG
snapshots. Generic stream requests and preloading cannot send the opaque source
to FFmpeg. The integration does not expose RTSP credentials, SDP, ICE candidates,
or the Doorfast bridge address in entity state. After upgrading, restart Home
Assistant to stop any ordinary stream workers created by an older version.

The go2rtc ingress and field acceptance procedure is documented in
[`docs/go2rtc-webrtc-acceptance.md`](docs/go2rtc-webrtc-acceptance.md). This
video path does not imply two-way audio or a confirmed physical door/elevator
action.

If go2rtc enables API authentication, open the Doorfast integration's options
and enter the go2rtc API username and password. Keep the API URL credential-free;
the integration sends Basic Auth only on its HA-to-go2rtc WebSocket connection.

## Listen or talk to an established call

The integration bundles and automatically registers a `doorfast-ptt-card` dashboard
card with three explicit modes. Use your own config entry and discovered station
ID in these examples; `one` / `gate_main` are synthetic fixture values. Adding a
card does not establish a call, and stopping audio does not hang up, unlock, or
stop video. Installation and dashboard changes require your deployment decision.

| Mode | Gesture | Audio |
| --- | --- | --- |
| `ptt` (default) | Hold the button or Space/Enter; release to stop | Microphone uplink only; `station_id` is optional for legacy configuration |
| `duplex` | Click to start; click again to stop | Continuous listening and microphone uplink when permission/security allow |
| `listen` | Click to start; click again to stop | Listening only; never requests the microphone |

For continuous intercom with the selected station already `talking`:

```yaml
type: custom:doorfast-ptt-card
config_entry_id: one
station_id: gate_main
mode: duplex
name: Main gate intercom
```

For listening only:

```yaml
type: custom:doorfast-ptt-card
config_entry_id: one
station_id: gate_main
mode: listen
name: Main gate audio
```

For the existing hold-to-talk behavior:

```yaml
type: custom:doorfast-ptt-card
config_entry_id: one
station_id: gate_main
mode: ptt
name: Doorfast Push to Talk
```

`duplex` and `listen` require `station_id`; an optional positive `generation`
must match the current backend call, otherwise omit it to bind to that call.
The browser requires an HTTPS Home Assistant URL or localhost for microphone
capture. On HTTP, `duplex` visibly falls back to listening without asking for a
microphone. Denying permission also leaves `duplex` listening only. PTT cannot
start on an insecure browser origin. Microphone capture never automatically
restarts after stopping, disconnecting, or changing permission: use a new click
or hold gesture when ready.

Losing focus, hiding the page, removing the card, ending the microphone track,
or losing the HA WebSocket stops the session and releases its playback nodes,
tracks, AudioContext, listener and producer. PTT also stops on button release.
Audio is downmixed to mono, filtered and resampled to
8 kHz signed 16-bit little-endian PCM in exact 20 ms frames. The browser sends
PCM and an opaque capture ID through HA's authenticated WebSocket; Doorfast producer
credentials stay in the integration backend.

Uplink sends at most five 320-byte frames per request, with one request in
flight and no replay of uncertain submissions. Downlink PCM is mono 8 kHz
`s16le`, at most 3200 bytes per chunk (canonical WAV at most 3244 bytes). The
HTTP reader rejects as soon as 4096 bytes have been consumed, without probing
byte 4097; accepted bodies are therefore **below** 4096 bytes. This does not
promise acceptance of arbitrary extended 4096-byte WAV files.

The output uses the AudioContext sample clock: startup is about 120 ms for
100 ms chunks, or a 100 ms lead for maximum 200 ms chunks, with the scheduled
end no more than 300 ms ahead. On overflow/discontinuity, a playing tail is
retained only if the incoming whole chunk can follow serially within that
budget; otherwise it is stopped and playback resynchronizes to current audio.
This is local buffering, not measured physical/end-to-end latency. Backend
source lag compares the selected chunk's bridge publication timestamp to the
latest publication timestamp, plus time in the HA cache; it does not measure
true capture delay.

| Feedback / backend code | What to do |
| --- | --- |
| Listening only: HTTPS required | Keep trusted-LAN listening, or open HA through HTTPS before starting again |
| Listening only: Microphone denied | Change browser permission if desired, then explicitly stop/start |
| `unsupported_audio_stream` | Update Doorfast to a bridge advertising `audio.pcm_stream_version: 1` |
| `audio_identity_mismatch` / `runtime_mismatch` | Verify the selected station is already talking; start a fresh audio session |
| `producer_busy` | Stop the existing producer before starting another microphone |
| `audio_cursor_expired` | The backend resynchronizes within the same identity; old speech is not replayed |
| `audio_connection_lost` / disconnected | Session resources are released; restore the connection and explicitly start again |

Authenticated administrator WebSocket access is required. The listener idle
lease and continuous invalid-result deadline are five seconds; a valid `304`
keeps the reader alive. Audio/WAV contents, producer tokens and HA credentials
must not be written to logs. After an authorized integration upgrade, restart
HA and reload open dashboards so all cached frontend modules use the same
resource version. See the [acceptance evidence levels](docs/ha-e2e-acceptance-runner.md)
before making any device-level duplex claim.

# 安装方式

## 使用 HACS 安装

[![在 HACS 中打开 Doorfast 仓库。](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jieinfo&repository=doorfastforha&category=integration)

也可在 HACS 的“集成”页面打开右上角菜单，选择“自定义存储库”，填入：

```text
https://github.com/jieinfo/doorfastforha
```

类型选择“Integration”。安装最新 Release 后重启 Home Assistant，再在“设置 → 设备与服务 → 添加集成”中搜索 `Doorfast`。HACS 使用 GitHub Release 的版本标签更新集成；版本说明见 [CHANGELOG.md](CHANGELOG.md)。

完成 Doorfast 集成配置后，在“设置 → 系统 → 日志”搜索 `Doorfast configured`。日志会显示可直接填入 Doorfast LuCI 的配置项 ID，以及事件 relay 路径。

## 手动安装

将 `custom_components/doorfast` 文件夹复制到 Home Assistant 的 `custom_components` 目录，然后重启 Home Assistant。

# 设置

[![打开 Home Assistant 并设置新的集成。](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=doorfast)
___

## 主动事件推送

集成保留配置中的 5 秒状态轮询作为断线和事件丢失时的兜底，同时提供受 Home Assistant 身份认证保护的 `POST /api/doorfast/<entry_id>` 入口。由于 Home Assistant 通常与 Doorfast 主机分开部署，HA 不能直接读取主机上的 Unix socket；Doorfast 侧需要增加一个事件转发器，向该 URL 发出 HTTP 或 HTTPS POST，并携带 Home Assistant 长期访问令牌。HTTP 仅应在可信内网使用。

事件 JSON 必须为以下格式：

```json
{"schema_version":1,"event_id":42,"event":"incoming_call","generation":7,"timestamp_ms":1710000000000}
```

允许的通话事件为 `incoming_call`、`call_established`、`hangup`、`timeout` 和 `preempted`；主动预览事件为 `monitor_requested`、`monitor_confirmed`、`monitor_media_ready`、`monitor_publishing`、`monitor_failed`、`monitor_stopped` 和 `monitor_preempted`。预览事件必须带有 `status_revision` 以及过滤后的 `status` 对象。集成会在发布 HA dispatcher 信号前刷新一次 `/api/v1/status`，拒绝格式错误、重复 `(generation,event)` 或旧 generation/revision 事件；Doorfast 的 `runtime_id` 变化时会重置通话和预览事件高水位。重复或旧事件返回 HTTP 202，状态刷新失败返回 HTTP 503，便于转发器稍后重试。

## 验收工具

`run_acceptance.py` 和 `doorfast_ha_e2e/` 提供测试专用的 HA/VM 验收 runner，覆盖配置入口、静态资源、音频 WebSocket 生命周期、断线清理、generation 隔离和重载。使用方式与证据边界见 [`docs/ha-e2e-acceptance-runner.md`](docs/ha-e2e-acceptance-runner.md)；该工具不会随集成运行时加载，也不替代 MT8157 实体设备验收。
