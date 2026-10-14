# Experimental integration artifact

Experimental integration `0.3.1.dev2` was prepared offline. Use the exact matching published prerelease and checksums, or a separately approved verified local candidate. Candidate preparation does not establish release or HACS availability. It is not a supported general installation. Packaging establishes neither HACS UI replacement, live Supervisor migration, stock-Core media playback, camera switching, authentication nor physical-device acceptance. See [INSTALL.md](INSTALL.md).

The source is a composite S1/T9000 baseline, not a byte-equivalent official upstream version. The integration version identifies this experimental release; historical component version labels do not identify official release inclusion. Existing contribution branches and upstream history remain separate. Relative to the exact public `0.3.1.dev1` baseline commit `8bfeaae54aae1f393fa4da4cb616440c22086a76`, the candidate runtime changes only detection-mask safety and version metadata. The existing endpoint reconfigure flow, entry/registry identifiers, write/report reconciliation and config schema remain unchanged.

## Candidate release notes: 0.3.1.dev2

- Non-bitmask detection reports now display unknown and both toggle directions refuse scalar writes over structured or unrecognized values.
- Numeric-string masks preserve all unrelated bits just like numeric masks. An absent value retains the existing fallback; bool, finite-float truncation and negative conversion behavior remain compatible.
- Malformed/nonfinite mask values use a controlled error without raw device report content. No automatic repair, hidden device query or authentication action was added.
- The identical new synthetic regression produced 55 failures and 27 passes against the unchanged baseline; the focused candidate suite passed 159 tests plus 55 subtests. Focused lint and formatting passed. Tests used HA 2026.10.1 in an isolated network-disabled container, with fake bridge I/O. These results qualify the narrow implementation; they do not establish a supported Core range, physical-device behavior, installation, new-account authentication or media acceptance.
- All six local Core overlays remain excluded. Stock-Core playback, camera switching, ARM and non-S1 operation remain unqualified. The paired SDK/bridge/add-on is separate and requires its own exact artifact/provenance review.

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
