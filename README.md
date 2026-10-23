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
