# Eufy SDK S1 Experimental (genomez)

Experimental S1/T9000 Home Assistant integration and optional player source.

Native WebRTC live view requires a matching bridge started with
`BRIDGE_WEBRTC_RELAY=1`. Only an explicit boolean `webrtcRelay: true` in the
bridge's WebSocket hello enables the native camera class, and only for streaming
cameras uniquely bound to a T9000 station. Relay-off, older bridges without this
advertisement, and other cameras retain the existing RTSP/HA stream path.
There is no separate integration switch. If the advertisement changes after a
bridge reconnect, the integration schedules one config-entry reload to recreate
camera classes; unchanged reconnects do not reload. A fresh setup consumes the
bridge hello before its auth/list replies and establishes a new baseline, so the
reload does not repeat. Capability listeners are removed when the entry unloads
or its client closes. No Home Assistant private capability attributes are changed.

**Experimental integration `0.3.1.dev2`.** Use the exact matching published prerelease and checksums, or a separately approved verified local candidate. Candidate preparation does not establish release or HACS availability. Its narrow detection-mask safety change and synthetic regression results are documented in [EXPERIMENTAL.md](EXPERIMENTAL.md). Packaging does not establish installation, authentication, physical-device or media acceptance. Read [INSTALL.md](INSTALL.md), [CORE_COMPATIBILITY.md](CORE_COMPATIBILITY.md), and [EXPERIMENTAL.md](EXPERIMENTAL.md) before a separate installation decision.

This is an independently maintained experimental branch derived from the [upstream project](https://github.com/mega-yfue/ha-eufy-sdk) and [danymexi's S1 work](https://github.com/danymexi/eufy-sdk). It is not an official upstream release. Upstream copyright and license notices are preserved.

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

Run `python -B tools/build_integration_zip.py --output-dir <empty-output-directory>` with Python 3.13 or later to reproduce the manual archive. It uses only the standard library, verifies a pinned file allowlist, and writes `eufy-sdk-s1-0.3.1.dev2.zip` plus hashes/content receipt. Fixed timestamps, permissions, order and stored compression make byte identity independent of checkout timestamps and zlib versions. No deployment, dependency downloads, registry edits or camera starts occur.

`SOURCE_MANIFEST.json` identifies this local candidate and its exact public baseline by file hashes; `PREVIEW_SOURCE_MANIFEST.json` is the unchanged historical source-preview manifest, not this candidate's current inventory. The narrow endpoint reconfigure flow preserves the existing entry and identifiers after successful bridge connection/authentication validation. It does not verify account identity or device equivalence; the migration checkpoint in INSTALL.md requires that review before changing endpoints.
