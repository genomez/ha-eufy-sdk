# Integration license and modifications

The integration is distributed under the MIT License in `LICENSE` and `custom_components/eufy_sdk/LICENSE`. Original copyright and license text are preserved without replacement. Derived work credits remain in README.md.

Historical baseline `0.3.1.dev1` is maintained by `genomez`, independently of upstream. Changes from the public S1 source preview are fork/install metadata, installation/compatibility/rollback documentation, reproducible packaging, and a narrow explicit endpoint reconfigure flow with translated UI and synthetic native-framework tests. The reconfigure flow preserves the existing entry and registry identities, validates connection/authentication before endpoint changes, rejects other-entry endpoint collisions and closes removed/failed validation connections.

No SDK/bridge runtime, bundled third-party Python library, native binary, add-on data or Core overlay is added by this integration package. Optional frontend assets are outside the integration artifact. Its prepared paired container has separate notices/provenance requirements.

Historical candidate `0.3.1.dev2` is based on public baseline commit `8bfeaae54aae1f393fa4da4cb616440c22086a76`. Its narrow detection-mask safety adaptation follows upstream commit `c59b3d93cde9f5e889cf10950b129ce1c389bba5` (mega-yfue/ha-eufy-sdk 0.5.1), retaining numeric masks and existing reconciliation while refusing unsafe values. Malformed/nonfinite conversions are handled and errors omit raw reports. Only version/package metadata, current candidate documentation/provenance and corresponding synthetic regressions accompany that implementation. This artifact was prepared offline; publication or packaging establishes no new runtime acceptance or supported Core range.

Local companion candidate `0.3.1.dev3` incorporates integration development through `3c53503` above public `0.3.1.dev2` commit `6d4402dca70c24560cc48860dd6affbae632f00d`. It adds native T9000 WebRTC signaling gated by bridge hello capability, preserves busy/releasing admission errors, debounces contrary floodlight reports for 8 seconds within a maximum 30-second command-settling window while retaining post-write reconciliation, and improves storage-attempt and enum presentation. Matching runtime `0.4.1-s1.experimental.6` is prepared separately. Synthetic tests and source preparation establish neither installation nor physical-device/media acceptance.

## Upstream integration ports, 2026-10-16

Substantial portions of `custom_components/eufy_sdk/preload.py`, `repairs.py`, the battery-worker/addition methods in `camera.py`, English preload issue strings, and `tests/test_camera_battery_stream.py` / `test_preload_repair.py` derive from mega-yfue/ha-eufy-sdk commits `bde4a566ea8549543b2c3346a077f91fdc106069` and `2ee7e77752340a87d54064ecc4b923165e15ddf0`. Alarm/arming missing-data guards adapt `3d4dd6b2e42a7150f1a7f1a4541cd7c6ca9c5702`; setup-event regression tests exercise the composite fork's existing callbacks. The code is adapted to the fork's constructor, missing-data lookup, native T9000 inheritance and synthetic fixtures. English issue strings are added without changing other translation values; that file's line endings are normalized to LF.

Copyright (c) 2026 mega-yfue. These upstream portions remain under the MIT License. The complete original copyright, permission grant, inclusion condition and warranty disclaimer are preserved in both `LICENSE` and `custom_components/eufy_sdk/LICENSE`, included in the deterministic package. No upstream release equivalence or endorsement is implied. Existing fork notices, native WebRTC and floodlight settling behavior are retained.

## Local integration review, 2026-10-17

Changes above local base `5833e03bca08ef98b8f749ba5ef38c0bb4bc0c8f` finish the 2026-10-16 `integration-siren-actions.patch` proposal: siren creation, availability and direct commands require a complete bound siren action descriptor; sounding state uses reported booleans only. Additional validation rejects malformed action members, duplicate descriptors and mismatched capability/accessor namespaces. The SDK remains responsible for supported hardware and command routing; no model allowlist is copied here.

Doorbell events retain the legacy `pressed` type and also publish Home Assistant's standard `ring` type, with one state write per type. Storage presentation reuses a validated observation's UTC datetime and skips validation/attempt-diagnostic work while disconnected. Other warned binary/push/select getters retain their existing bounded scalar reads. Synthetic regression and timing checks cover these paths. The offline runner adapts the retained local no-I/O test-loop approach; no runtime dependency is added.

Changed Python source/test/helper files carry dated modification notices. Original copyright, MIT license and upstream attribution remain intact. The reviewed runtime source remains unchanged in the local release candidate. Its deterministic packaging and current version are recorded below; source review does not establish Home Assistant 2026.10 qualification or physical-device acceptance.

## Floodlight pending-success presentation, 2026-10-17

Changes above local base `74ad50a` make a successful latest `light` property write present its requested state within the existing 30-second settling window, including a successful bridge response that is awaiting late device confirmation. Fresh contrary reports or observation receipts received during the RPC start the existing 8-second correction timer when the call succeeds. Unchanged real reads still update the shared cache and receipt timestamp; later observations or the settling deadline restore the last real state. Generic property report-ordering and latest-write ownership safeguards remain in place. No new wire metadata or runtime dependency is added.

Six added offline test items and two added subtests cover pending-equivalent success, unchanged contrary readings before and after RPC completion, matching observations and the no-read deadline. Existing overlap, lifecycle and non-light ordering tests remain applicable. These local checks do not qualify Home Assistant 2026.10 or physical-device behavior.

## Release package refresh, 2026-10-17

Integration `0.3.1.dev3`, paired with runtime `0.4.1-s1.experimental.6`, is prepared from reviewed source commit `0f4ff70` (implementation `c50fe57`). Implementation files, translations and tests are unchanged. This refresh updates candidate documentation, publication provenance and pinned package/source inventories, then builds a deterministic manual ZIP. No installation, authentication, dependency installation, publication or newly supported Core range is claimed.
