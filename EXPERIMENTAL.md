# Experimental integration artifact

Experimental integration `0.3.1.dev3` is a local companion candidate for runtime `0.4.1-s1.experimental.6`. Use the exact matching published prerelease and checksums, or a separately approved verified local candidate. Candidate preparation does not establish release or HACS availability. Packaging establishes neither installation, authentication, stock-Core playback, camera switching nor physical-device acceptance. See [INSTALL.md](INSTALL.md).

The source is a composite S1/T9000 baseline, not a byte-equivalent official upstream version. The integration version identifies this experimental candidate; historical component labels do not identify official release inclusion. Existing contribution branches and upstream history remain separate. This candidate incorporates local integration development through `3c53503` on top of published `0.3.1.dev2` commit `6d4402dca70c24560cc48860dd6affbae632f00d`. The config schema and existing entry/registry identifiers are retained.

## Candidate release notes: 0.3.1.dev3

- Native Home Assistant WebRTC live view forwards offers and ICE candidates to the bridge for streaming cameras uniquely bound to a T9000 station. Only the explicit boolean `webrtcRelay: true` in the bridge WebSocket hello selects the native camera class. Relay-off, missing advertisements and other cameras retain the RTSP/HA stream path. The matching bridge requires `BRIDGE_WEBRTC_RELAY=1`; there is no separate integration switch.
- A relay advertisement change after reconnect schedules one config-entry reload to recreate camera classes. Unchanged advertisements do not reload; a fresh setup establishes a new hello baseline. Listeners and signaling sessions are cleaned up on unload/removal or transport loss.
- Bridge admission errors retain `webrtc_busy` and `webrtc_releasing`, even when an early close event precedes the open reply. No automatic retry or hidden stream start is added.
- After a successful floodlight command, a short settling guard prevents brief contrary reports from making the displayed light flicker. A contrary boolean report starts an 8-second debounce timer; agreement cancels that timer. Regardless of the remaining debounce time, the guard ends no later than 30 seconds after the command, even if refreshes fail. The normal post-write refresh remains 20 seconds. Reports still update the cache and observation receipts; the displayed intent is not physical-device confirmation. New commands and entity removal retire stale settling timers.
- Storage diagnostics distinguish off, waiting and failed attempts, retain bounded failure categories and tolerate a finite fractional next-attempt timestamp from older bridges. Unlabelled enum codes display their raw value.
- Offline discovery with the existing local Home Assistant environment ran 314 unittest cases: 312 passed, with the same firmware-registry assertion failure and reconfigure-registry API error documented for the inherited baseline. The debounce change adds 19 discovered regression cases. The full suite is not green; these checks do not establish runtime, installation or media acceptance. Pytest-only functions are not executed by unittest discovery.
- All six local Core overlays remain excluded. Stock-Core playback, camera switching, ARM and non-S1 operation remain unqualified. This integration contains neither the paired runtime nor optional frontend installation.

## Historical 0.3.1.dev2 change

The earlier candidate adapted detection-mask safety from upstream commit `c59b3d93cde9f5e889cf10950b129ce1c389bba5`, retaining unrelated bits while refusing unsafe structured or unrecognized values. Its retained qualification reported 159 focused tests plus 55 subtests with fake bridge I/O in an isolated network-disabled HA 2026.10.1 container. Those historical results are not a fresh validation of this candidate.

## Historical baseline evidence

The private qualification target was Linux amd64. ARM and non-S1 operation are not supported by this preview. Source and offline test results are distinct from installation and physical-device acceptance.

Historical qualification retained from the earlier baseline: the prepared SDK suite passed 4,946 tests and the bridge suite passed 95 tests. Integration qualification included 220 test items plus 155 subtests and a separate 16-case native framework suite. These are retained pre-publication qualification results, not checks newly run by public CI on this branch. Publication metadata and documentation edits are reviewed separately.

Historical baseline observations: the private bridge installation completed and basic single-camera playback worked. Camera switching and intermittent frontend resets remain unresolved. A lifecycle trace observed an active player element being replaced by a new element; the trigger is not yet identified. Reported light state has also differed from observed physical state.

The tested streaming environment used six separate, version-specific Home Assistant Core modifications on a 2026.10.1 basis. They are not bundled here. Stock-Core streaming compatibility is unverified. Do not overwrite Core files or assume an ordinary HACS install recreates that environment. The optional player assets are not automatically registered or added to dashboards.

## Installation boundary

Do not replace a working installation with the earlier source preview. A future separately approved installation of this candidate requires the backup/identity checkpoint in [INSTALL.md](INSTALL.md). The integration retains `eufy_sdk` and cannot coexist with another owner of that domain. The endpoint reconfigure flow checks connection/authentication, not account/device identity. Add-on repository identity affects data directories and discovery; migration has not been validated for other installations. There is no promised public binary rollback target: preserve exact prior component, coherent configuration/registry snapshot, sole HACS/manual owner and paired bridge/add-on recovery material.

The bridge's historical registry SDK dependency is not the customized SDK used during qualification. Running `npm ci` in the source-preview bridge alone does not produce the qualified pair. Use only the separately reviewed exact paired experimental bridge/add-on image and corresponding-source/notices assets; this integration ZIP bundles neither. Verify that paired release's registry digest and source receipts. Add-on migration/rollback and live acceptance remain separate installation work.

No workflow in this branch publishes packages, images or releases. Native dependency provenance and binary corresponding-source/notices are supplied with the separate paired container release, not this integration artifact. Existing licenses and upstream credit are preserved.

## Contributing useful findings

Source reviews, synthetic reproductions and narrowly scoped fixes are welcome. For a bug report, include the component commit, model numbers (not serial numbers), software versions, power type, connection topology in general terms, reproduction steps, and expected versus observed behavior.

Do not post credentials, tokens, session files, account identifiers, device serials, P2P identifiers, personal names, home addresses, IP/MAC/SSID values, household dashboards, camera images, packet captures or raw logs. Start with a short redacted description. Use synthetic fixtures for code and tests. Keep private recovery material outside repositories and build contexts.

Fixes should retain explicit stream start/stop, cleanup on navigation/removal/visibility changes, and ordinary state-read/session-renewal behavior. No automatic retries or hidden camera starts should be added to hide a failure.

Target contributions at `s1-experimental`. The existing default branch belongs to the earlier fork history; its workflows and support claims are not those of this source preview. The inherited HACS minimum-version metadata is not a qualified compatibility range for this preview.
