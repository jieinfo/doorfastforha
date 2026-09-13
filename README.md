# Doorfast for Home Assistant

This integration is a clean Home Assistant adapter for Doorfast host mode. It covers door unlock, elevator up/down calls, incoming-call notifications, hangup and optional video.

The integration talks to a small JSON HTTP bridge on the Doorfast host. The bridge maps requests to the local `ubus` object and exposes `GET /api/v1/status`, `POST /api/v1/unlock`, `/api/v1/answer`, `/api/v1/hangup` and `/api/v1/call_elevator`. The adapter uses the Doorfast host API directly.

Doorfast status is authoritative for command acceptance, while physical door/elevator confirmation remains a separate status field and is shown as returned by the bridge.

# 安装方式

## 使用 HACS 安装

[![打开 Home Assistant 并打开 HACS商店内的存储库。](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=xswxm&repository=[doorfast](https://github.com/xswxm/doorfast)&category=integration)

## 手动安装

将 `custom_components/doorfast` 文件夹复制到 Home Assistant 的 `custom_components` 目录，然后重启 Home Assistant。

# 设置

[![打开 Home Assistant 并设置新的集成。](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=doorfast)
___
