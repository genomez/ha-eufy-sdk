# S1 experimental.9 — Integration notices

Home Assistant custom integration for the S1 experimental stack. Install the paired integration ZIP or use HACS Redownload, then restart Home Assistant.

Pair add-on `0.4.1-s1.experimental.9`, integration/bridge `0.3.1-s1.experimental.9` and SDK `0.2.1-s1.experimental.9`.

The integration ZIP includes one integration domain and its license/notices. It contains no frontend overlay or additional recovery service. Device-reported state can be delayed; diagnostics do not prove physical state.

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

## Modification and attribution notices

Copyright (c) 2026 mega-yfue. These upstream portions remain under the MIT License. The complete original copyright, permission grant, inclusion condition and warranty disclaimer are preserved in both `LICENSE` and `custom_components/eufy_sdk/LICENSE`, included in the deterministic package. No upstream release equivalence or endorsement is implied. Existing fork notices, native WebRTC and floodlight settling behavior are retained.

## Files modified in experimental.9

Modified for genomez experimental.9, 2026-10-27.

- `CORE_COMPATIBILITY.md`
- `EXPERIMENTAL.md`
- `INSTALL.md`
- `INTEGRATION_NOTICES.md`
- `INTEGRATION_PACKAGE.json`
- `PUBLICATION.json`
- `README.md`
- `SOURCE_MANIFEST.json`
- `custom_components/eufy_sdk/camera_health.py`
- `custom_components/eufy_sdk/light_correction.py`
- `custom_components/eufy_sdk/manifest.json`
- `tests/test_camera_health.py`
- `tests/test_off_stuck_escalation.py`
- `tests/test_slow_reply_isolation.py`
- `tools/build_integration_zip.py`

## Historical experimental.8 documentation

# Experimental.8 verified public delivery

Runtime/source release: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.8
Integration release: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.8
Verified registry manifest: sha256:4289f94e617c0e60207edb8ddaa75d88cfdf777ad081e9e43feb915a4b2ef119
Paired add-on 0.4.1-s1.experimental.8, integration/bridge 0.3.1-s1.experimental.8 and SDK 0.2.1-s1.experimental.8. These bindings come from independent anonymous release, asset, workflow and registry verification. Installation, HACS and physical-device acceptance remain separate.

The official integration ZIP remains frozen at 6bb1365de39615d9e094b44b7665118451326e8d; current branch documentation may differ from the release archive. Version-specific source, image-context and archive records below remain historical. The exact prepared implementation and workflow are unchanged.

## Historical local preparation and earlier release records

# Experimental.8 local preparation

Version `0.3.1-s1.experimental.8` is prepared locally and unpublished. Pair add-on `0.4.1-s1.experimental.8`, integration/bridge `0.3.1-s1.experimental.8` and SDK `0.2.1-s1.experimental.8`.

RTC late-viewer keyframe gating; bounded refresh backoff and recovery; isolated slow replies; camera connectivity and hub reply evidence; T8171 detection; opt-in floodlight safety with bounded confirmation and problem notices; observation-only media stall evidence.

Live view remains off by default; only boolean `webrtc_relay: true` enables it. Command keepalive remains 29000 ms. Integration floodlight safety uses `light_correction_enabled` and remains off by default. The opt-in safety binds HA intent, uses 15-minute motion and ON waits with a 30-minute watchdog, retries OFF until camera confirmation within its limits, and reports problems with bounded phone retries. It may retry an authorized ON intent; the historical .7 OFF-only corrector description below is superseded. Passive connectivity diagnostics do not establish physical state.

Media gap evidence is observation only; no automatic stream restart is added. T8113-Z firmware 3.3.8 picture-then-black remains unresolved. A second live-view open may still be needed. Brightness/spotlight formatting and unexplained floodlight ON events remain under investigation. The refresh retry/backoff addresses the prior .7 refresh issue.

This is an offline local candidate, with no release, registry, installation, account, device, media or new soak acceptance. Update and installation instructions in the historical text apply only after a separately approved publication.

## Historical published experimental.7 documentation

# Experimental.7 published delivery

Integration release: https://github.com/genomez/ha-eufy-sdk/releases/tag/0.3.1-s1.experimental.7. Runtime release: https://github.com/genomez/ha-eufy-sdk-addon/releases/tag/s1-runtime-0.4.1.experimental.7. HACS installation remains unverified. Reproduce the official ZIP from release tag `0.3.1-s1.experimental.7`, commit `b3922d91fcd73a10b763b69dfcf1c4d72ccc7a1f`.

## Historical preparation record

# Integration license and modifications

## Local experimental.7 package refresh, 2026-10-22

Current version `0.3.1-s1.experimental.7` uses integration input `689ee3c33b995734085e99089f5fb1ce7605c176`, a descendant of experimental.6 `071c55bf4a9f606eea7603ff3946a52965b2e774`. Implementation, translations and tests are retained byte-for-byte from that input. Only release documentation, the component version, deterministic builder version and current package/source/publication inventories are refreshed.

Changes since experimental.6 include app-aligned preset selection and removal of separate Go buttons; bounded light writes and reported-state presentation; the optional OFF-only integration-owned corrector, command-health suppression, original-authority cooldown recovery, confirmation/resume and motion-aware grace; and the input's existing configurable single doorbell event and native offer wait. The approved doorbell follow-up advertises ring in legacy pressed mode while still sending only pressed, for Home Assistant 2027.4; default ring mode is unchanged. Paired SDK changes are outside this package.

Original MIT licenses, copyright and upstream attribution are preserved byte-for-byte. The historical source-preview manifest remains unchanged. The current inventory compares every tracked path except itself with the exact published experimental.6 baseline, including new implementation and test files. Earlier inventories and review descriptions remain historical provenance, not current-byte claims.

This preparation is local and unpublished, with no runtime/add-on packaging or live acceptance.

## Historical notices retained from experimental.6 and earlier


The integration is distributed under the MIT License in `LICENSE` and `custom_components/eufy_sdk/LICENSE`. Original copyright and license text are preserved without replacement. Derived work credits remain in README.md.

Historical baseline `0.3.1.dev1` is maintained by `genomez`, independently of upstream. Changes from the public S1 source preview are fork/install metadata, installation/compatibility/rollback documentation, reproducible packaging, and a narrow explicit endpoint reconfigure flow with translated UI and synthetic native-framework tests. The reconfigure flow preserves the existing entry and registry identities, validates connection/authentication before endpoint changes, rejects other-entry endpoint collisions and closes removed/failed validation connections.

No SDK/bridge runtime, bundled third-party Python library, native binary, add-on data or Core overlay is added by this integration package. Optional frontend assets are outside the integration artifact. Its prepared paired container has separate notices/provenance requirements.

Historical candidate `0.3.1.dev2` is based on public baseline commit `8bfeaae54aae1f393fa4da4cb616440c22086a76`. Its narrow detection-mask safety adaptation follows upstream commit `c59b3d93cde9f5e889cf10950b129ce1c389bba5` (mega-yfue/ha-eufy-sdk 0.5.1), retaining numeric masks and existing reconciliation while refusing unsafe values. Malformed/nonfinite conversions are handled and errors omit raw reports. Only version/package metadata, current candidate documentation/provenance and corresponding synthetic regressions accompany that implementation. This artifact was prepared offline; publication or packaging establishes no new runtime acceptance or supported Core range.

Local companion candidate `0.3.1-s1.experimental.6` incorporates integration development through `3c53503` above public `0.3.1.dev2` commit `6d4402dca70c24560cc48860dd6affbae632f00d`. It adds native T9000 WebRTC signaling gated by bridge hello capability, preserves busy/releasing admission errors, debounces contrary floodlight reports for 8 seconds within a maximum 30-second command-settling window while retaining post-write reconciliation, and improves storage-attempt and enum presentation. Matching runtime `0.4.1-s1.experimental.6` is prepared separately. Synthetic tests and source preparation establish neither installation nor physical-device/media acceptance.

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

Integration `0.3.1-s1.experimental.6`, paired with runtime `0.4.1-s1.experimental.6`, is prepared from reviewed source commit `0f4ff70` (implementation `c50fe57`). Implementation files, translations and tests are unchanged. This refresh updates candidate documentation, publication provenance and pinned package/source inventories, then builds a deterministic manual ZIP. No installation, authentication, dependency installation, publication or newly supported Core range is claimed.


## Historical input documentation and modification notices

# Integration license and modifications

The integration is distributed under the MIT License in `LICENSE` and `custom_components/eufy_sdk/LICENSE`. Original copyright and license text are preserved without replacement. Derived work credits remain in README.md.

Historical baseline `0.3.1.dev1` is maintained by `genomez`, independently of upstream. Changes from the public S1 source preview are fork/install metadata, installation/compatibility/rollback documentation, reproducible packaging, and a narrow explicit endpoint reconfigure flow with translated UI and synthetic native-framework tests. The reconfigure flow preserves the existing entry and registry identities, validates connection/authentication before endpoint changes, rejects other-entry endpoint collisions and closes removed/failed validation connections.

No SDK/bridge runtime, bundled third-party Python library, native binary, add-on data or Core overlay is added by this integration package. Optional frontend assets are outside the integration artifact. Its prepared paired container has separate notices/provenance requirements.

Historical candidate `0.3.1.dev2` is based on public baseline commit `8bfeaae54aae1f393fa4da4cb616440c22086a76`. Its narrow detection-mask safety adaptation follows upstream commit `c59b3d93cde9f5e889cf10950b129ce1c389bba5` (mega-yfue/ha-eufy-sdk 0.5.1), retaining numeric masks and existing reconciliation while refusing unsafe values. Malformed/nonfinite conversions are handled and errors omit raw reports. Only version/package metadata, current candidate documentation/provenance and corresponding synthetic regressions accompany that implementation. This artifact was prepared offline; publication or packaging establishes no new runtime acceptance or supported Core range.

Local companion candidate `0.3.1-s1.experimental.6` incorporates integration development through `3c53503` above public `0.3.1.dev2` commit `6d4402dca70c24560cc48860dd6affbae632f00d`. It adds native T9000 WebRTC signaling gated by bridge hello capability, preserves busy/releasing admission errors, debounces contrary floodlight reports for 8 seconds within a maximum 30-second command-settling window while retaining post-write reconciliation, and improves storage-attempt and enum presentation. Matching runtime `0.4.1-s1.experimental.6` is prepared separately. Synthetic tests and source preparation establish neither installation nor physical-device/media acceptance.

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

Integration `0.3.1-s1.experimental.6`, paired with runtime `0.4.1-s1.experimental.6`, is prepared from reviewed source commit `0f4ff70` (implementation `c50fe57`). Implementation files, translations and tests are unchanged. This refresh updates candidate documentation, publication provenance and pinned package/source inventories, then builds a deterministic manual ZIP. No installation, authentication, dependency installation, publication or newly supported Core range is claimed.
