# Eufy SDK S1 Experimental (genomez)

Experimental S1/T9000 Home Assistant integration and optional player source.

**Experimental installer release `0.3.1.dev1`; installation and live acceptance remain separate steps.** Use the matching [GitHub prerelease](https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1.dev1) and its exact source/artifact receipts. Select exact prerelease `0.3.1.dev1` in HACS; older HACS versions may require **Show beta versions**. Its installation/replacement UI remains unverified. Read [INSTALL.md](INSTALL.md), [CORE_COMPATIBILITY.md](CORE_COMPATIBILITY.md), and [EXPERIMENTAL.md](EXPERIMENTAL.md) before a separately approved install.

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

Run `python -B tools/build_integration_zip.py --output-dir <empty-output-directory>` with Python 3.13 or later to reproduce the manual archive. It uses only the standard library, verifies a pinned file allowlist, and writes `eufy-sdk-s1-0.3.1.dev1.zip` plus hashes/content receipt. Fixed timestamps, permissions, order and stored compression make byte identity independent of checkout timestamps and zlib versions. No deployment, dependency downloads, registry edits or camera starts occur.

`SOURCE_MANIFEST.json` identifies this experimental release source by file hashes; `PREVIEW_SOURCE_MANIFEST.json` preserves the public preview's earlier manifest. The narrow endpoint reconfigure flow preserves the existing entry and identifiers after successful bridge connection/authentication validation. It does not verify account identity or device equivalence; the migration checkpoint in INSTALL.md requires that review before changing endpoints.
