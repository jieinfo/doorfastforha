# Doorfast WebRTC Preview Acceptance

This guide validates the video path after Doorfast host mode and the HA
integration are installed. It describes the ingress contract only; it does
not replace an on-site MT8157 call or a packet capture.

For multi-station deployments, configure one `streams` entry per station
stream name. `max_encoders` limits station source sessions, not browser
viewers sharing a source. The repository fixture exercises these contracts
synthetically and is not evidence of concurrent physical door stations.

## Boundary and configuration

Doorfast publishes the encoded preview to the HA host's go2rtc RTSP ingress.
The producer name is fixed and opaque:

```yaml
api:
  listen: ":1984"
rtsp:
  listen: ":8554"
webrtc:
  listen: ":8555"
streams:
  doorfast_preview:
    - rtsp://DOORFAST_RTSP_USER:${DOORFAST_RTSP_PASSWORD}@immortal-wrt.example:8554/doorfast_preview
```

The exact RTSP URL and credential values are deployment values. If the go2rtc
API requires HTTP Basic Auth, enter the API username and password in the
Doorfast integration's options flow together with the API base URL. The
password is used only for HA-to-go2rtc WebSocket signaling and is not included
in the URL, Doorfast camera entity, HA state, browser configuration, or logs.
Doorfast connects to the HA RTSP ingress on `8554/TCP`. HA and the browser use
the local go2rtc API on `1984` for WebSocket signaling and the configured
WebRTC listener on `8555`. Doorfast must not access the go2rtc API or construct
SDP, ICE, or browser signaling messages.

The HA source remains the opaque value
`doorfast://<config_entry_id>/station/<station_id>/preview`. The integration
opens the configured go2rtc API WebSocket from the HA host. A remote HA
deployment must expose that API through its own trusted network path; do not
put credentials in the camera source URL.

## Acceptance sequence

1. Confirm the media module is installed and available in the Doorfast status.
   Start one monitor generation from the `doorfast.start_monitor` service or
   by opening the camera. The camera entity must advertise WebRTC only while
   media is available.
2. Confirm the go2rtc producer list contains exactly one producer named
   `doorfast_preview` after the monitor reaches `publishing`. Record the redacted
   producer snapshot and the Doorfast media status revision.
3. Open the camera from one HA browser and one HA mobile client. Both viewers
   must receive valid H.264 video through WebRTC while the single producer
   remains active. No Doorfast RTSP URL, password, SDP, or candidate may appear
   in HA state, diagnostics, or browser-visible configuration.
4. Close the last viewer and confirm the Doorfast monitor viewer flag is
   released. After the configured grace period, the exact generation must stop;
   a new viewer must create or reuse only the current generation.
5. Trigger an incoming call while preview is active. Confirm monitor
   preemption closes the old go2rtc session immediately, clears its generation,
   and leaves the call path available. A later preview must use a new
   generation and must not show a frame from the old call.
6. Run a ten-minute stability check with one producer and two viewers. Record
   monitor state/revision, go2rtc producer/consumer counts, HA logs, memory
   usage, and any relay failures. Repeat once with a viewer reconnect.

## Evidence and limits

The acceptance checklist includes incoming call preemption and viewer
reconnection.

Retain redacted Doorfast status, HA logs, go2rtc producer/consumer snapshots,
and an anonymized pcap showing the Doorfast-to-HA `8554/TCP` ingress and the
HA-local signaling path. Remove credentials, tokens, SDP, ICE candidates,
query strings, and private addresses before sharing evidence.

This acceptance proves the video transport and generation lifecycle. It does not validate two-way audio. Microphone capture and a physical door/elevator result remain separate PCM/control acceptance paths.
