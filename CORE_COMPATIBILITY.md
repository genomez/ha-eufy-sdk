# S1 experimental.15

Pair: add-on **0.4.1-s1.experimental.15**, integration/bridge **0.3.1-s1.experimental.15**, SDK **0.2.1-s1.experimental.15**.

## New
- Late face names: when HomeBase recognises a face a minute or more after the person alert, that camera's Last person sensor changes from "Human" to the name, for that same detection only. An `eufy_sdk_person_identified` event is also fired for automations.

## Fixed
- Arm/disarm is confirmed by reading the mode back; a request lost during a reconnect is settled instead of reported as failed, and a contradiction is only reported after a completed read-back.
- Floodlight correction alerts fire only when a light stays blocked from turning off, and no longer have gaps after a recovery.
- Cameras are no longer removed when the HomeBase returns a partial device list.
- Repeated copies of the same detection no longer trigger again.
- Recording day folders show the correct year.

## Changed
- Much less recorder database traffic: attributes stay stable between updates and refresh timestamps are no longer recorded.
- Last person sensors consistently say "Human" for unrecognised people and show more detection details.
- Investigation diagnostics are off by default.

## Upgrade
Update the add-on and integration together, then restart Home Assistant after the add-on is running.

## Known limits
Not every recognised face gets a late name: sometimes HomeBase has no matching record within 15 minutes, and when two people appear in separate recordings only one name may show. Playback is silent; multi-lens cameras show one view.

## Historical public documentation through experimental.14

# S1 experimental.14 — Home Assistant compatibility

## .14 changes

- Fixed: S350 (T8416) cameras no longer show a spotlight switch they don't have. A push connection that silently stops delivering events is now detected and reconnected. Camera settings are no longer overwritten by older values when a startup or refresh overlaps a change. Snapshot refreshes are no longer lost when they overlap; queued refreshes stop after a sign-in failure.
- Changed: Floodlight brightness/spotlight controls that HomeBase S1 can't drive are hidden. Number settings use per-model ranges. Media → Eufy shows "Playback not yet supported for this camera", "No playable recordings yet" or "No recordings yet" instead of an empty camera folder. More playback diagnostics.
- Upgrade: Update the add-on and integration together, then restart Home Assistant after the add-on is running, so removed controls are cleaned up.
- Known limits: Playback is silent; multi-lens cameras show one view; a late update from a camera can still briefly show an old state.

# S1 experimental.12

Update the add-on and integration together: add-on `0.4.1-s1.experimental.12`, integration and bridge `0.3.1-s1.experimental.12`, SDK `0.2.1-s1.experimental.12`.

- Push-store save failures are caught and reported through push-connection diagnostics (#539).
- Session and FCM files use an owner-only temporary file and atomic rename where the filesystem permits (#540). On restricted directories or single-file mounts, the existing in-place fallback is non-atomic and retains existing file permissions.
- Stored snapshots recover after a successful re-login, including when an older in-flight download fails afterwards (#545).
- Optional HomeBase S1 storage telemetry also supports firmware 4.4.1.4; it remains off by default.
- After an acknowledged light command, Home Assistant holds the requested display value for up to 45 seconds. A matching boolean report releases it early. The acknowledgement does not confirm the physical light state.
- A registry identity conflict invalidates the affected child-camera status to unknown.
- HomeBase firmware metadata updates from newly received inventory without a Home Assistant restart. A new Firmware version sensor exposes the received version.
- Dated experimental.11 and experimental.12 modification notices are retained, including the restored integration notice.

Defaults are unchanged. Live view and optional floodlight safety remain off by default. Keep battery-camera preload off. Verify the integration ZIP checksum and restart Home Assistant after replacing the integration. Changing live-view opt-in requires an add-on restart. Command keepalive remains 29000 ms.

Known live-view stalls, unexplained floodlight ON events and brightness/spotlight formatting issues remain unresolved. This is an experimental fork.

Runtime and corresponding source: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.12

Integration ZIP and checksum: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.12

Publication availability is pending. The accepted image is delivered without rebuilding. Earlier release records below are historical.

Modified for genomez experimental.11, 2026-10-31

Modified for genomez experimental.12, 2026-11-02

## Historical documentation before experimental.12 publication

# S1 experimental.11 — Home Assistant compatibility

Home Assistant custom integration for the S1 experimental stack. Use the paired integration ZIP or HACS Redownload, then restart Home Assistant.

Pair add-on `0.4.1-s1.experimental.11`, integration/bridge `0.3.1-s1.experimental.11` and SDK `0.2.1-s1.experimental.11`.

This release is experimental. Offline regression tests use Home Assistant 2026.7.0; they do not establish compatibility with every Home Assistant release or camera firmware. Check Home Assistant logs after updating.

## Changes

- Diagnostics keep bounded failure-only command and property-write history, so failures remain visible when later commands succeed. The SDK error history is limited to 32 entries.
- Event thumbnail decoding supports the V2 encrypted-chroma variant. Alarm pushes that explicitly report an app stop are labelled stopped rather than triggered.
- Unsupported RTSP stream publish controls are hidden for cameras behind HomeBase S1. RTSP URLs and the existing recording/authentication settings retain their existing support rules.
- Streaming-quality sensors behind HomeBase S1 appear only when the camera has reported the corresponding quality parameter. The separate video-quality select is unchanged.
- After updating both components and restarting Home Assistant, 15 existing RTSP stream switches and 15 existing streaming-quality sensors on the affected installation are expected to become unavailable. Their retained entity entries can be removed in Home Assistant. Streaming-quality sensors with reported support remain available.
- Camera-health diagnostics show the last suppressed signal-strength reading, its reason and source. This diagnostic does not change the published signal strength or prove the current radio state.
- More cameras now show Online/Offline status from HomeBase S1 (T9000). Offline/recovery testing covers T8425, T8426, T8416, T8214, T8134 and T8170; T8171 has not yet been offline-tested.
- Camera status holds until the HomeBase reports a change.
- After a HomeBase reconnect, cameras show unknown until the next routine status update, about 10 minutes by default plus polling and sweep time. Failed or disabled polling or missing reports can leave status unknown longer.
- A camera marked offline never holds back light OFF commands or retries. Existing transport and safety checks still apply. Upgrade the add-on and integration together.

## Defaults and use

Defaults are unchanged. Live view is off by default; only boolean `webrtc_relay: true` enables it. Changing that option requires an add-on restart. Command keepalive remains 29000 ms. Integration `light_correction_enabled` remains off. With safety off, OFF-stuck incident reporting is inactive and its four attributes are absent. No new option or default was added. Keep battery-camera preload off.

## Known issues

- T8113-Z firmware 3.3.8 can show a picture then black.
- Some live-view sessions need another open or wait for the hub offer.
- Unexplained floodlight ON events and brightness/spotlight command formatting remain unresolved. This update does not claim to fix these issues. Passive diagnostics do not establish physical state.

## Releases, source and notices

Runtime and corresponding source: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.11

Integration release and ZIP checksum: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.11

Image manifest: `sha256:28cc3774423aae12a7a328c0d39c66c317f8c5232466073c356076d1bbd50471`.

See LICENSE and source headers for applicable license and author notices. Runtime/source archives retain their original notices and corresponding-source links.

Full SDK corresponding source: https://github.com/genomez/ha-eufy-sdk-addon/releases/download/s1-runtime-0.4.1.experimental.11/eufy-sdk-0.2.1-s1.experimental.11-source.tar.gz; SHA-256 `d49311318a9522d7cdc6ebb0bddaeaa12bd7d8df383688b414ed294c2b03d465`, 1619648 bytes. The npm package is separate. In-image source records retain their historical build-time values.

## Historical experimental.9 / experimental.10 documentation

# S1 experimental.10 — Home Assistant compatibility

Home Assistant custom integration for the S1 experimental stack. Install the integration ZIP or use HACS Redownload for this version, then restart Home Assistant.

Integration `0.3.1-s1.experimental.10` requires add-on 0.4.1-s1.experimental.9 (unchanged). Bridge `0.3.1-s1.experimental.9` and SDK `0.2.1-s1.experimental.9` are unchanged. This is an integration-only update; keep the existing add-on.

This release is experimental. Offline regression tests use Home Assistant 2026.7.0; they do not establish compatibility with every Home Assistant release or camera firmware. Check Home Assistant logs after updating.

## Changes

- With optional floodlight safety enabled, a light last confirmed off no longer opens a false "no reports" OFF-stuck incident after a Home Assistant restart or bridge reconnect. Incident closures are recorded after startup.
- Pan/tilt acknowledgement timeouts now explain: "Pan/tilt command sent, but the camera didn't confirm in time; it may still have moved." The acknowledgement timeout and retry behavior are unchanged; this message does not confirm physical movement.
- Event images refresh on new camera events and image-update signals, and once per completed device snapshot, instead of on every camera-health update. Availability recovery still refreshes the image; identical image data stays quiet.
- The "Recording mode" select is now named "RTSP recording mode" to clarify that it controls RTSP/NAS recording, not HomeBase continuous recording. Existing entity IDs are unchanged.

## Defaults and use

Defaults are unchanged. Live view is off by default; only boolean `webrtc_relay: true` enables it. Changing that option requires an add-on restart. Command keepalive remains 29000 ms. Integration `light_correction_enabled` remains off. With safety off, OFF-stuck incident reporting is inactive and its four attributes are absent. No new option or default was added. Keep battery-camera preload off.

## Known issues

- T8113-Z firmware 3.3.8 can show a picture then black.
- Some live-view sessions need another open or wait for the hub offer.
- Unexplained floodlight ON events and brightness/spotlight command formatting remain unresolved. This update does not claim to fix these issues. Passive diagnostics do not establish physical state.

## Releases, source and notices

Runtime and corresponding source: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.9

Integration release and ZIP checksum: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.10

See LICENSE and source headers for applicable license and author notices. Runtime/source archives retain their original notices and corresponding-source links.

## Historical experimental.9 documentation

# S1 experimental.9 — Home Assistant compatibility

Home Assistant custom integration for the S1 experimental stack. Install the paired integration ZIP or use HACS Redownload, then restart Home Assistant.

Pair add-on `0.4.1-s1.experimental.9`, integration/bridge `0.3.1-s1.experimental.9` and SDK `0.2.1-s1.experimental.9`.

This release is experimental. Use the paired versions and check your Home Assistant logs after updating. Offline tests do not establish compatibility with every Home Assistant release or camera firmware.

## Changes

- Failed SDK background read-through refreshes report through the existing `state_refresh` origin and retain last-known values until an update succeeds.
- Routine camera health attributes refresh less often. On one installation, 15 connectivity sensors went from about 317,000 recorder rows a day to about 1,600.
- With the existing optional floodlight safety enabled, unconfirmed OFF incidents expose `off_stuck_incident`, `off_stuck_reason`, `off_stuck_opened_at` and `off_stuck_escalate_at`.
- SDK Markdown formatting was corrected. A test-only change uses 5.0015 seconds to avoid clock rounding; production timing is unchanged.

## Defaults and use

Live view is off by default; only boolean `webrtc_relay: true` enables it. Live-view opt-in requires an add-on restart. Command keepalive remains 29000 ms. Integration `light_correction_enabled` remains off. With safety off, OFF-stuck incident reporting is inactive and its four attributes are absent. No new option or default was added. Keep battery-camera preload off.

## Known issues

T8113-Z firmware 3.3.8 can show a picture then black. Some live-view sessions need another open or wait for the hub offer. Unexplained floodlight ON events and brightness/spotlight command formatting remain unresolved. This release does not claim to fix these issues. Passive diagnostics do not establish physical state.

## Releases, source and notices

Runtime and corresponding source: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.9

Integration release and ZIP checksum: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.9

Image manifest: `sha256:fe2fca682ecf41655402abc7ba2da970995eeed4a6010f51865409b60141bd0e`. The integration release archive corresponds to source commit `bf6d218faa3df8734c8bfee594ed6ae6e807e1f3`; current branch documentation can differ from that tagged archive.

See LICENSE and source headers for applicable license and author notices. Runtime/source archives retain their original notices and immutable base corresponding-source links.

## Historical experimental.8 documentation

# Experimental.8 local preparation

Version `0.3.1-s1.experimental.8` is described in this historical notice. Pair add-on `0.4.1-s1.experimental.8`, integration/bridge `0.3.1-s1.experimental.8` and SDK `0.2.1-s1.experimental.8`.

RTC late-viewer keyframe gating; bounded refresh backoff and recovery; isolated slow replies; camera connectivity and hub reply evidence; T8171 detection; opt-in floodlight safety with bounded confirmation and problem notices; observation-only media stall evidence.

Live view remains off by default; only boolean `webrtc_relay: true` enables it. Command keepalive remains 29000 ms. Integration floodlight safety uses `light_correction_enabled` and remains off by default. The opt-in safety binds HA intent, uses 15-minute motion and ON waits with a 30-minute watchdog, retries OFF until camera confirmation within its limits, and reports problems with bounded phone retries. It may retry an authorized ON intent; the historical .7 OFF-only corrector description below is superseded. Passive connectivity diagnostics do not establish physical state.

Media gap evidence is observation only; no automatic stream restart is added. T8113-Z firmware 3.3.8 picture-then-black remains unresolved. A second live-view open may still be needed. Brightness/spotlight formatting and unexplained floodlight ON events remain under investigation. The refresh retry/backoff addresses the prior .7 refresh issue.

This is an offline local candidate, with no release, registry, installation, account, device, media or new soak acceptance. Update and installation instructions in the historical text apply only after a separately approved publication.

## Historical published experimental.7 documentation

# Core compatibility boundary

The private installation's Core basis was **2026.10.1 with six local overlays**. Native framework qualification is narrower evidence than stock-Core media acceptance. This integration candidate includes none of those six Core files and never installs or overwrites Core.

| Item | Candidate status |
| --- | --- |
| Integration files and MIT license | Included |
| Paired SDK/bridge/add-on | Separate package |
| Six local Core overlays | Excluded |
| Optional scoped WebRTC player | Source-only; excluded from ZIP and ordinary HACS install |
| Stock-Core streaming / media playback | Unqualified |
| Camera switching | Unresolved / unqualified |
| ARM and non-S1 installations | Unqualified |
| General supported Core version range | Not established |

Inherited HACS floors `homeassistant: 2026.6.4` and `hacs: 2.0.5` are retained as metadata constraints. They are **not a tested support range** for this fork, do not describe the six private overlays, and do not qualify stock-Core compatibility. A version allowed by HACS is not an accepted environment. No new supported minimum/version range is asserted.

A working private environment must preserve and independently back up its exact Core image/version and modifications. This integration ZIP cannot restore them. Do not deploy overlays through this package or assume HACS reproduces the private streaming environment. A Core update and integration replacement require separate recovery decisions.

References, reviewed 2026-10-14: [HACS manifest keys](https://www.hacs.xyz/docs/publish/start/#hacsjson), [Home Assistant custom integration versions](https://developers.home-assistant.io/docs/creating_integration_manifest/#version).
