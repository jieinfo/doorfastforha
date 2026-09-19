# Changelog

## 0.3.1

- Retry Home Assistant WebRTC negotiation while the go2rtc producer is starting.
- Cancel in-flight retries cleanly when a viewer disconnects.
- Avoid transient signaling errors and unretrieved Future exceptions during startup.

## 0.2.0

- Add active Doorfast monitor lifecycle controls and Home Assistant native WebRTC signaling through local go2rtc.
- Share one Doorfast H.264 source stream across multiple Home Assistant viewers and cleanly release the monitor when viewers disconnect.
- Bind answer, hangup, unlock, and elevator controls to the current Doorfast `runtime_id`.
- Reset call and preview event high-water marks when the Doorfast daemon restarts.
- Prevent in-flight video and audio responses from restoring stale media after a runtime change.

## 0.1.1

- Log the Doorfast configuration entry ID and event relay path after successful setup.

## 0.1.0

- Initial HACS release of the Doorfast Home Assistant integration.
- Door unlock, elevator calls, incoming-call events, video snapshots, and generation-bound audio transport.
