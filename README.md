# Doorfast for Home Assistant

This integration is a clean Home Assistant adapter for Doorfast host mode. It covers door unlock, elevator up/down calls, incoming-call notifications, hangup and optional video.

The integration talks to the Doorfast HTTP bridge at `http://<host>/cgi-bin/doorfast`. The bridge maps requests to the local `ubus` object and exposes `GET /api/v1/status`, `POST /api/v1/unlock`, `/api/v1/answer`, `/api/v1/hangup` and `/api/v1/call_elevator`. Enter the host URL in the setup form; the CGI path is added automatically.

Doorfast status is authoritative for command acceptance, while physical door/elevator confirmation remains a separate status field and is shown as returned by the bridge.

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

# 安装方式

## 使用 HACS 安装

[![打开 Home Assistant 并打开 HACS商店内的存储库。](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=xswxm&repository=[doorfast](https://github.com/xswxm/doorfast)&category=integration)

## 手动安装

将 `custom_components/doorfast` 文件夹复制到 Home Assistant 的 `custom_components` 目录，然后重启 Home Assistant。

# 设置

[![打开 Home Assistant 并设置新的集成。](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=doorfast)
___

## 主动事件推送

集成保留配置中的 5 秒状态轮询作为断线和事件丢失时的兜底，同时提供受 Home Assistant 身份认证保护的 `POST /api/doorfast/<entry_id>` 入口。由于 Home Assistant 通常与 Doorfast 主机分开部署，HA 不能直接读取主机上的 Unix socket；Doorfast 侧需要增加一个事件转发器，向该 URL 发出 HTTPS POST，并携带 Home Assistant 长期访问令牌。

事件 JSON 必须为以下格式：

```json
{"schema_version":1,"event_id":42,"event":"incoming_call","generation":7,"timestamp_ms":1710000000000}
```

允许的事件为 `incoming_call`、`call_established`、`hangup`、`timeout` 和 `preempted`。集成会在发布 HA dispatcher 信号前刷新一次 `/api/v1/status`，拒绝格式错误、重复 `(generation,event)` 或旧 generation 事件；重复或旧事件返回 HTTP 202，状态刷新失败返回 HTTP 503，便于转发器稍后重试。
