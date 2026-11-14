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

# S1 experimental.14

## .14 changes

- Fixed: S350 (T8416) cameras no longer show a spotlight switch they don't have. A push connection that silently stops delivering events is now detected and reconnected. Camera settings are no longer overwritten by older values when a startup or refresh overlaps a change. Snapshot refreshes are no longer lost when they overlap; queued refreshes stop after a sign-in failure.
- Changed: Floodlight brightness/spotlight controls that HomeBase S1 can't drive are hidden. Number settings use per-model ranges. Media → Eufy shows "Playback not yet supported for this camera", "No playable recordings yet" or "No recordings yet" instead of an empty camera folder. More playback diagnostics.
- Upgrade: Update the add-on and integration together, then restart Home Assistant after the add-on is running, so removed controls are cleaned up.
- Known limits: Playback is silent; multi-lens cameras show one view; a late update from a camera can still briefly show an old state.

SDK 0.2.1-s1.experimental.14; bridge/integration 0.3.1-s1.experimental.14; add-on 0.4.1-s1.experimental.14. Linux amd64.

Recorded playback is progressive by default. Extra camera models are an eligibility opt-in, not proof of codec or lens compatibility. Keep the previous add-on/integration pair and backups for rollback; restore both together and restart Home Assistant. Live view and floodlight safety remain opt-in; keep battery preload off. Command keepalive is 29000 ms.

## Historical documentation through experimental.12

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

# S1 experimental.11 — Overview

Home Assistant custom integration for the S1 experimental stack. Use the paired integration ZIP or HACS Redownload, then restart Home Assistant.

Pair add-on `0.4.1-s1.experimental.11`, integration/bridge `0.3.1-s1.experimental.11` and SDK `0.2.1-s1.experimental.11`.

Update the add-on and integration together and retain your existing configuration. This is an experimental fork, not an official upstream release.

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

# S1 experimental.10 — Overview

Home Assistant custom integration for the S1 experimental stack. Install the integration ZIP or use HACS Redownload for this version, then restart Home Assistant.

Integration `0.3.1-s1.experimental.10` requires add-on 0.4.1-s1.experimental.9 (unchanged). Bridge `0.3.1-s1.experimental.9` and SDK `0.2.1-s1.experimental.9` are unchanged. This is an integration-only update; keep the existing add-on.

Update only the integration. Keep add-on `0.4.1-s1.experimental.9` and your existing configuration. This is an experimental fork, not an official upstream release.

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

# S1 experimental.9 — Overview

Home Assistant custom integration for the S1 experimental stack. Install the paired integration ZIP or use HACS Redownload, then restart Home Assistant.

Pair add-on `0.4.1-s1.experimental.9`, integration/bridge `0.3.1-s1.experimental.9` and SDK `0.2.1-s1.experimental.9`.

Use the matching release versions and retain your existing configuration unless deliberately changing an option.

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

# Experimental.8 verified public delivery

Runtime/source release: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.8
Integration release: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.8
Verified registry manifest: sha256:4289f94e617c0e60207edb8ddaa75d88cfdf777ad081e9e43feb915a4b2ef119
Paired add-on 0.4.1-s1.experimental.8, integration/bridge 0.3.1-s1.experimental.8 and SDK 0.2.1-s1.experimental.8. These bindings come from independent anonymous release, asset, workflow and registry verification. Installation, HACS and physical-device acceptance remain separate.

The official integration ZIP remains frozen at 6bb1365de39615d9e094b44b7665118451326e8d; current branch documentation may differ from the release archive. Version-specific source, image-context and archive records below remain historical. The exact prepared implementation and workflow are unchanged.

## Historical local preparation and earlier release records

# Experimental.8 local preparation

Version `0.3.1-s1.experimental.8` is described in this historical notice. Pair add-on `0.4.1-s1.experimental.8`, integration/bridge `0.3.1-s1.experimental.8` and SDK `0.2.1-s1.experimental.8`.

RTC late-viewer keyframe gating; bounded refresh backoff and recovery; isolated slow replies; camera connectivity and hub reply evidence; T8171 detection; opt-in floodlight safety with bounded confirmation and problem notices; observation-only media stall evidence.

Live view remains off by default; only boolean `webrtc_relay: true` enables it. Command keepalive remains 29000 ms. Integration floodlight safety uses `light_correction_enabled` and remains off by default. The opt-in safety binds HA intent, uses 15-minute motion and ON waits with a 30-minute watchdog, retries OFF until camera confirmation within its limits, and reports problems with bounded phone retries. It may retry an authorized ON intent; the historical .7 OFF-only corrector description below is superseded. Passive connectivity diagnostics do not establish physical state.

Media gap evidence is observation only; no automatic stream restart is added. T8113-Z firmware 3.3.8 picture-then-black remains unresolved. A second live-view open may still be needed. Brightness/spotlight formatting and unexplained floodlight ON events remain under investigation. The refresh retry/backoff addresses the prior .7 refresh issue.

This is an offline local candidate, with no release, registry, installation, account, device, media or new soak acceptance. Update and installation instructions in the historical text apply only after a separately approved publication.

## Historical published experimental.7 documentation

# Experimental.7 published delivery

Integration release: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.7. Runtime release: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.7. HACS installation remains unverified. Reproduce the official ZIP from release tag `0.3.1-s1.experimental.7`, commit `b3922d91fcd73a10b763b69dfcf1c4d72ccc7a1f`.

## Historical preparation record

# Eufy SDK S1 Experimental (genomez)

Experimental S1/T9000 Home Assistant integration and optional player source.

Native WebRTC live view requires a matching bridge started with
`BRIDGE_WEBRTC_RELAY=1`. Only an explicit boolean `webrtcRelay: true` in the
bridge's WebSocket hello enables the native camera class, and only for streaming
cameras uniquely bound to a T9000 station. Relay-off, older bridges without this
advertisement, and other cameras retain the existing RTSP/HA stream path.
Native signaling forwards offers and ICE candidates through the bridge and closes sessions on removal or transport loss. Admission refusals retain `webrtc_busy` or `webrtc_releasing`, including when a close event arrives before the open reply. These labels report bridge ownership; they do not trigger an automatic retry.

There is no separate integration switch. If the advertisement changes after a
bridge reconnect, the integration schedules one config-entry reload to recreate
camera classes; unchanged reconnects do not reload. A fresh setup consumes the
bridge hello before its auth/list replies and establishes a new baseline, so the
reload does not repeat. Capability listeners are removed when the entry unloads
or its client closes. No Home Assistant private capability attributes are changed.

**Experimental integration `0.3.1-s1.experimental.7`.** This is a local unpublished candidate; release and HACS availability have not been established. Its native WebRTC signaling, floodlight state reconciliation, storage diagnostics and synthetic regression results are documented in [EXPERIMENTAL.md](EXPERIMENTAL.md). Packaging does not establish installation, authentication, physical-device or media acceptance. Read [INSTALL.md](INSTALL.md), [CORE_COMPATIBILITY.md](CORE_COMPATIBILITY.md), and [EXPERIMENTAL.md](EXPERIMENTAL.md) before a separate installation decision.

This is an independently maintained experimental branch derived from the [upstream project](https://github.com/mega-yfue/ha-eufy-sdk) and [danymexi's S1 work](https://github.com/danymexi/eufy-sdk). It is not an official upstream release. Upstream copyright and license notices are preserved.

Floodlight command acceptance and reported physical state remain distinct. Optional automatic correction is owned by the integration, is off by default, and uses fresh confirmation plus bounded writes. Motion restarts a 300-second grace period for an uncommanded on episode, up to a 900-second episode cap. Passive diagnostics remain available while correction is off. See [EXPERIMENTAL.md](EXPERIMENTAL.md).

This candidate is the integration companion prepared for runtime `0.4.1-s1.experimental.7`. The matching runtime artifact and relay configuration require separate verification; this repository does not bundle them.

## Component sources

- [sdk](https://github.com/genomez/eufy-sdk/tree/s1-experimental)
- [bridge](https://github.com/genomez/ha-eufy-sdk-bridge/tree/s1-experimental)
- [integration](https://github.com/genomez/ha-eufy-sdk/tree/s1-experimental)
- [addon](https://github.com/genomez/ha-eufy-sdk-addon/tree/s1-experimental)

The SDK and bridge metadata retain historical version labels. The source manifest identifies this preview by file hashes; those labels must not be used to infer official release inclusion.

Please open source issues in this fork, using a redacted description or synthetic reproduction. Avoid raw logs and screenshots containing personal or device information.

## Installation ownership and package

This fork is owned by `@genomez` and retains the upstream `eufy_sdk` domain. Use exactly one owner for `custom_components/eufy_sdk`: this fork in HACS or a manual install. Another repository with the same domain cannot run alongside it. Replacing component files does not authorize removing the existing Home Assistant config entry.

The manual ZIP contains the integration plus MIT license and install documentation. It excludes the bridge, SDK, add-on, tests, frontend assets, private configuration and all six local Core overlays. Stock-Core media/playback and camera switching remain unqualified. Optional `frontend/scoped-webrtc-camera` files remain source-only and are not registered or added to dashboards by HACS or this ZIP.

Run `python -B tools/build_integration_zip.py --output-dir <empty-output-directory>` with Python 3.13 or later to reproduce the manual archive. It uses only the standard library, verifies a pinned file allowlist, and writes `eufy-sdk-s1-0.3.1-s1.experimental.7.zip` plus hashes/content receipt. Fixed timestamps, permissions, order and stored compression make byte identity independent of checkout timestamps and zlib versions. No deployment, dependency downloads, registry edits or camera starts occur.

`SOURCE_MANIFEST.json` identifies this local candidate and its exact public baseline by file hashes; `PREVIEW_SOURCE_MANIFEST.json` is the unchanged historical source-preview manifest, not this candidate's current inventory. The narrow endpoint reconfigure flow preserves the existing entry and identifiers after successful bridge connection/authentication validation. It does not verify account identity or device equivalence; the migration checkpoint in INSTALL.md requires that review before changing endpoints.

## Local unpublished experimental.7, 2026-10-22

Integration version `0.3.1-s1.experimental.7` is prepared from exact input `689ee3c33b995734085e99089f5fb1ce7605c176` above published experimental.6 `071c55bf4a9f606eea7603ff3946a52965b2e774`. This preparation is local, unpublished and not installed; no HACS availability is claimed. Runtime/add-on preparation and availability are separate and unverified here.

- Automatic correction is OFF-only and never re-sends ON. Shared SDK command health pauses automatic writes after repeated fast refusals; explicit user commands remain eligible. Cooldown recovery retains the original correction deadline and send limits.
- Presets use app positions 1-4. Choosing a position in the select moves the camera; the separate Go buttons are removed. Movement controls require advertised movement support.
- The optional integration floodlight corrector is the sole owner of automatic correction. It remains off by default and depends on the paired bridge/SDK bounded confirmation protocol. It uses fresh observations, a finite command window, at most three corrective writes and a pause helper. A successful command still does not prove physical state.
- When the last HA intent is off and the floodlight comes on without an HA command, correction waits 300 seconds. Fresh motion restarts that grace period, capped at 900 seconds for the episode. Motion also protects a pending command-owned off correction. Fresh reads, cancellation, expiry and send limits still apply; app or motion changes cannot always be distinguished.
- Passive observation and correction diagnostics remain available with automatic correction off. They do not themselves issue corrective commands or establish hardware acceptance.
- The doorbell event entity always advertises the standard ring type, including the legacy pressed option, which still sends only pressed, for Home Assistant's 2027.4 requirement. Default mode still advertises and sends ring. The longer native offer wait is retained.
- Paired SDK renewal busy cooldown is separate SDK work. This integration package does not contain or validate that runtime change; coordination and runtime verification remain pending.

Local unpublished integration 0.3.1-s1.experimental.7, exact input 689ee3c33b995734085e99089f5fb1ce7605c176. Fresh offline tests at the unmodified input: 684 passed, 0 failures, 450 passed subtests; HA 2026.7.0, Python 3.14.4. Preparation changes documentation, version and inventories only. No installation, device, media, HACS or runtime acceptance is established.

The sections explicitly marked historical below preserve earlier qualification, copyright and provenance. Earlier dual-event doorbell and requested-state presentation descriptions are historical and do not override this input's implementation. Current file inventories are in SOURCE_MANIFEST.json and INTEGRATION_PACKAGE.json.


## Historical input documentation and modification notices

# Eufy SDK S1 Experimental (genomez)

Experimental S1/T9000 Home Assistant integration and optional player source.

Native WebRTC live view requires a matching bridge started with
`BRIDGE_WEBRTC_RELAY=1`. Only an explicit boolean `webrtcRelay: true` in the
bridge's WebSocket hello enables the native camera class, and only for streaming
cameras uniquely bound to a T9000 station. Relay-off, older bridges without this
advertisement, and other cameras retain the existing RTSP/HA stream path.
Native signaling forwards offers and ICE candidates through the bridge and closes sessions on removal or transport loss. Admission refusals retain `webrtc_busy` or `webrtc_releasing`, including when a close event arrives before the open reply. These labels report bridge ownership; they do not trigger an automatic retry.

There is no separate integration switch. If the advertisement changes after a
bridge reconnect, the integration schedules one config-entry reload to recreate
camera classes; unchanged reconnects do not reload. A fresh setup consumes the
bridge hello before its auth/list replies and establishes a new baseline, so the
reload does not repeat. Capability listeners are removed when the entry unloads
or its client closes. No Home Assistant private capability attributes are changed.

**Experimental integration `0.3.1-s1.experimental.6`.** Use the exact matching published prerelease and checksums, or a separately approved verified local candidate. Candidate preparation does not establish release or HACS availability. Its native WebRTC signaling, floodlight state reconciliation, storage diagnostics and synthetic regression results are documented in [EXPERIMENTAL.md](EXPERIMENTAL.md). Packaging does not establish installation, authentication, physical-device or media acceptance. Read [INSTALL.md](INSTALL.md), [CORE_COMPATIBILITY.md](CORE_COMPATIBILITY.md), and [EXPERIMENTAL.md](EXPERIMENTAL.md) before a separate installation decision.

This is an independently maintained experimental branch derived from the [upstream project](https://github.com/mega-yfue/ha-eufy-sdk) and [danymexi's S1 work](https://github.com/danymexi/eufy-sdk). It is not an official upstream release. Upstream copyright and license notices are preserved.

After a successful floodlight command, brief contrary reports are debounced to reduce displayed state flicker. Contrary boolean reports use an 8-second debounce within a settling window that ends at most 30 seconds after the command, even if a refresh fails. The normal post-write refresh remains 20 seconds. The display alone does not prove physical state; see [EXPERIMENTAL.md](EXPERIMENTAL.md).

This candidate is the integration companion prepared for runtime `0.4.1-s1.experimental.6`. The matching runtime artifact and relay configuration require separate verification; this repository does not bundle them.

## Component sources

- [sdk](https://github.com/genomez/eufy-sdk/tree/s1-experimental)
- [bridge](https://github.com/genomez/ha-eufy-sdk-bridge/tree/s1-experimental)
- [integration](https://github.com/genomez/ha-eufy-sdk/tree/s1-experimental)
- [addon](https://github.com/genomez/ha-eufy-sdk-addon/tree/s1-experimental)

The SDK and bridge metadata retain historical version labels. The source manifest identifies this preview by file hashes; those labels must not be used to infer official release inclusion.

Please open source issues in this fork, using a redacted description or synthetic reproduction. Avoid raw logs and screenshots containing personal or device information.

## Installation ownership and package

This fork is owned by `@genomez` and retains the upstream `eufy_sdk` domain. Use exactly one owner for `custom_components/eufy_sdk`: this fork in HACS or a manual install. Another repository with the same domain cannot run alongside it. Replacing component files does not authorize removing the existing Home Assistant config entry.

The manual ZIP contains the integration plus MIT license and install documentation. It excludes the bridge, SDK, add-on, tests, frontend assets, private configuration and all six local Core overlays. Stock-Core media/playback and camera switching remain unqualified. Optional `frontend/scoped-webrtc-camera` files remain source-only and are not registered or added to dashboards by HACS or this ZIP.

Run `python -B tools/build_integration_zip.py --output-dir <empty-output-directory>` with Python 3.13 or later to reproduce the manual archive. It uses only the standard library, verifies a pinned file allowlist, and writes `eufy-sdk-s1-0.3.1-s1.experimental.6.zip` plus hashes/content receipt. Fixed timestamps, permissions, order and stored compression make byte identity independent of checkout timestamps and zlib versions. No deployment, dependency downloads, registry edits or camera starts occur.

`SOURCE_MANIFEST.json` identifies this local candidate and its exact public baseline by file hashes; `PREVIEW_SOURCE_MANIFEST.json` is the unchanged historical source-preview manifest, not this candidate's current inventory. The narrow endpoint reconfigure flow preserves the existing entry and identifiers after successful bridge connection/authentication validation. It does not verify account identity or device equivalence; the migration checkpoint in INSTALL.md requires that review before changing endpoints.
