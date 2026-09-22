# Changelog

## 0.3.9

- Authenticate Home Assistant WebRTC signaling against go2rtc APIs that require Basic Auth.
- Reload the integration when go2rtc connection options change and validate credential pairs.

## 0.3.8

- Prevent stale monitor polls and events from invalidating or reviving an active camera generation during WebRTC startup.

## 0.3.7

- Keep retrying WebRTC negotiation while slow Doorfast stations bring up their RTSP producer.

## 0.3.6

- Wait up to 25 seconds for Doorfast monitor startup so stations can recover from repeated busy responses before WebRTC negotiation begins.

## 0.3.5

- Keep the typed `STREAM` capability required for Home Assistant's native Doorfast WebRTC provider.

## 0.3.4

- Keep the camera feature flags as a `CameraEntityFeature` value so Home Assistant can register Doorfast cameras correctly.

## 0.3.3

- Route Doorfast camera sources exclusively through the native WebRTC provider instead of Home Assistant's generic stream worker.

## 0.3.2

- Recover the monitor lifecycle when Home Assistant WebRTC viewers reconnect during a stopping transition.
- Treat an idempotent Doorfast monitor stop response as successful during cleanup.

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
