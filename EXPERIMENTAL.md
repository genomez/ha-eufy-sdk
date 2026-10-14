# Experimental source preview

This branch is for source review and development. It is not a supported installation or a published binary release. No npm package, container image, HACS release or add-on-store release accompanies it.

The source is a composite S1/T9000 baseline, not a byte-equivalent copy of an official upstream version. Inherited version fields identify its preparation inputs; they do not identify a downloadable fork release. Existing contribution branches and upstream history remain separate.

## Scope and evidence

The private qualification target was Linux amd64. ARM and non-S1 operation are not supported by this preview. Source and offline test results are distinct from installation and physical-device acceptance.

The prepared SDK suite passed 4,946 tests and the bridge suite passed 95 tests. Integration qualification included 220 test items plus 155 subtests and a separate 16-case native framework suite. These are retained pre-publication qualification results, not checks newly run by public CI on this branch. Publication metadata and documentation edits are reviewed separately.

The private bridge installation completed and basic single-camera playback worked. Camera switching and intermittent frontend resets remain unresolved. A lifecycle trace observed an active player element being replaced by a new element; the trigger is not yet identified. Reported light state has also differed from observed physical state.

The tested streaming environment used six separate, version-specific Home Assistant Core modifications on a 2026.10.1 basis. They are not bundled here. Stock-Core streaming compatibility is unverified. Do not overwrite Core files or assume an ordinary HACS install recreates that environment. The optional player assets are not automatically registered or added to dashboards.

## Installation boundary

Do not replace a working installation with this source preview. The integration retains the upstream `eufy_sdk` domain and cannot be installed alongside another copy under that domain. Add-on repository identity affects data directories and discovery; migration has not been validated for other installations. There is no public binary rollback target in this preview.

The bridge's historical registry SDK dependency is not the customized SDK used during qualification. Running `npm ci` in the bridge alone does not produce the qualified component pair. The add-on recipe requires exact paired build inputs which are not a public install bundle. A coherent public dependency lock, distributable images, migration instructions and rollback procedure will accompany a later installable release.

No workflow in this branch publishes packages, images or releases. Native dependency provenance and binary corresponding-source/notices requirements remain separate release work. Existing licenses and upstream credit are preserved.

## Contributing useful findings

Source reviews, synthetic reproductions and narrowly scoped fixes are welcome. For a bug report, include the component commit, model numbers (not serial numbers), software versions, power type, connection topology in general terms, reproduction steps, and expected versus observed behavior.

Do not post credentials, tokens, session files, account identifiers, device serials, P2P identifiers, personal names, home addresses, IP/MAC/SSID values, household dashboards, camera images, packet captures or raw logs. Start with a short redacted description. Use synthetic fixtures for code and tests. Keep private recovery material outside repositories and build contexts.

Fixes should retain explicit stream start/stop, cleanup on navigation/removal/visibility changes, and ordinary state-read/session-renewal behavior. No automatic retries or hidden camera starts should be added to hide a failure.

Target contributions at `s1-experimental`. The existing default branch belongs to the earlier fork history; its workflows and support claims are not those of this source preview. The inherited HACS minimum-version metadata is not a qualified compatibility range for this preview.
