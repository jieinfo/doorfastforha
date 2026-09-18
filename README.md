# Doorfast for Home Assistant

This integration is a clean Home Assistant adapter for Doorfast host mode. It covers door unlock, elevator up/down calls, incoming-call notifications, hangup and optional video.

The integration talks to the Doorfast HTTP bridge at `http://<host>/cgi-bin/doorfast`. The bridge maps requests to the local `ubus` object and exposes `GET /api/v1/status`, `POST /api/v1/unlock`, `/api/v1/answer`, `/api/v1/hangup` and `/api/v1/call_elevator`. Enter the host URL in the setup form; the CGI path is added automatically.

Doorfast status is authoritative for command acceptance, while physical door/elevator confirmation remains a separate status field and is shown as returned by the bridge. The client reads the current 16-character `runtime_id` from status and includes it in answer, hangup, unlock and elevator requests together with the applicable call generation. A daemon restart changes `runtime_id`; cached media state and call-event high-water marks are then discarded so a delayed command or event cannot collide with a reused generation.

The lock entity sends a momentary unlock command and remains shown as locked because Doorfast does not yet receive a physical door-position signal. Its attributes expose the protocol state, generation, raw reply status and `physical_result_confirmed` value reported by the bridge.

The camera requests snapshots for the current call generation and reuses a
cached frame only when the Doorfast bridge returns `304 Not Modified` or a
short-lived `503 Service Unavailable`. A call-generation change, unavailable
video status, or `404`/`409` response clears the cached image so a previous
visitor cannot appear in a later call.

The client consumes Doorfast audio as generation-bound WAV chunks. It records
the revision returned by each successful request and asks for the next retained
chunk with that revision as its cursor. An unchanged response is not replayed,
temporary failures preserve the cursor for retry, and an expired cursor or call
generation change resets the sequence before resynchronizing. This client
boundary prepares continuous playback without allowing audio from an earlier
call into the current one; the integration does not yet expose a live audio
player entity.

## Active WebRTC preview

When Doorfast reports the media module as installed and available, the camera
entity exposes an opaque `doorfast://<config_entry_id>/preview` source. HA's
native `CameraWebRTCProvider` proxies signaling through HA-local go2rtc while
Doorfast publishes the H.264 preview to the go2rtc RTSP ingress. Use the
`doorfast.start_monitor` and `doorfast.stop_monitor` services for explicit
monitor control; camera viewers also acquire and release a generation
automatically. The provider keeps the JPEG snapshot path as a fallback and
does not expose RTSP credentials, SDP, ICE candidates, or the Doorfast bridge
address in entity state.

The go2rtc ingress and field acceptance procedure is documented in
[`docs/go2rtc-webrtc-acceptance.md`](docs/go2rtc-webrtc-acceptance.md). This
video path does not imply two-way audio or a confirmed physical door/elevator
action.

## Push to talk card

The integration bundles and automatically registers a `doorfast-ptt-card` dashboard
card. After installing or upgrading the integration, restart Home Assistant and
reload an already-open dashboard once. Add a manual card with the configuration:

```yaml
type: custom:doorfast-ptt-card
config_entry_id: YOUR_DOORFAST_CONFIG_ENTRY_ID
name: Doorfast Push to Talk
```

Press and hold the button (or hold Space/Enter while it is focused) to transmit.
The browser requires an HTTPS Home Assistant URL or localhost and prompts for
microphone access on the first press. Releasing the button, losing focus, hiding
the page, disconnecting the card, ending the microphone track, or losing the HA
WebSocket stops capture. Audio is downmixed to mono, filtered and resampled to
8 kHz signed 16-bit little-endian PCM in exact 20 ms frames. The browser sends
only an opaque capture ID through HA's authenticated WebSocket; Doorfast producer
credentials stay in the integration backend.

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
