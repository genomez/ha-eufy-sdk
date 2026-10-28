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

Version `0.3.1-s1.experimental.8` is prepared locally and unpublished. Pair add-on `0.4.1-s1.experimental.8`, integration/bridge `0.3.1-s1.experimental.8` and SDK `0.2.1-s1.experimental.8`.

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
