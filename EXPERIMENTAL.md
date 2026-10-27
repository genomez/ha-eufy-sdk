# Experimental.9 local preparation

Version `0.3.1-s1.experimental.9` is prepared locally and unpublished. Pair add-on `0.4.1-s1.experimental.9`, integration/bridge `0.3.1-s1.experimental.9` and SDK `0.2.1-s1.experimental.9`.

Failed SDK background read-through refreshes now report through the existing `state_refresh` origin and retain last-known values. Routine camera health attributes refresh less often to reduce recorder writes. Optional floodlight safety reports persisted unconfirmed OFF incidents through `off_stuck_incident`, `off_stuck_reason`, `off_stuck_opened_at` and `off_stuck_escalate_at`.

Defaults are unchanged from .8: relay off, enabled only by boolean `webrtc_relay: true`; command keepalive 29000 ms; `light_correction_enabled` off. With safety off, the new OFF-stuck reporting is inactive and its attributes are not exposed. No new option or default is added. SDK documentation formatting is a documentation-only change.

Known issues carried from .8: T8113-Z firmware 3.3.8 picture-then-black; live view sometimes needs another open or stalls awaiting the hub offer; unexplained floodlight ON events; deferred brightness/spotlight formatting. No fix for these is claimed. Keep battery-camera preload off.

Local offline validation does not establish new device, account, media, HACS or soak acceptance. Publication, installation and tester messaging need separate owner approval. Owner-local recovery automation is outside this release.

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

# Experimental integration artifact

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

## Optional contrary light correction

The integration options include **Correct confirmed contrary lights to OFF only**
(`light_correction_enabled`, default off) and an optional **Pause light corrections
helper** (`light_correction_pause_entity`, no helper by default). The pause selector
accepts an `input_boolean` or `switch`. A selected helper pauses corrections while
on, unavailable, or unknown; off resumes eligible work. Observation events and
attributes remain available with automatic correction disabled.

This requires a bridge advertising `lateContraryEffects` version 1, a 600000 ms
watch, confirmation support, and bounded state writes. An older bridge keeps its
existing write behavior. Only a new explicit HA light command establishes correction
authority, including commands issued by HA automations. A newer command immediately
replaces the previous lineage. Automatic correction supports OFF intent only.
ON recovery uses the original command and existing bounded SDK retry; ON
observations remain available with an `on_observation_only` suppression reason.

The latest explicit command owns one monotonic ten-minute window. A fresh SDK
confirmation of contrary state schedules corrections at 15, 60, and 300 seconds from
the first confirmation. Each send requires a fresh qualified SDK read, and sends
are at least 45 seconds apart, including overdue work after a pause. Corrections
retain the original window and maximum three-send budget. A reported match cancels
pending work while retaining the watch for a later reversal. The original expiry
travels with each correction and bounds queued transport sends. Ordinary SDK
recovery must settle before a fresh read and correction; correction-origin writes
cannot create an additional SDK recovery child.

SDK `commandHealth` receipts, events, and device snapshots share one per-light
command gate. `command_degraded` cancels pending automatic corrections while
retaining passive observation. Explicit HA commands still receive one normal SDK
attempt; the SDK suppresses automatic retry while degraded. A healthy receipt,
health event, or bounded cooldown expiry allows eligible OFF work to resume within
its original window and remaining budget. `command_health` describes the command
path, not device availability or confirmed physical light state. Optional SDK health
revisions order receipts, events, and snapshots; older or subsequently unversioned
projections cannot clear a newer gate. Equal revisions cannot extend cooldown.
Disconnect/ready resets ordering for a replacement SDK runtime while retaining only
the remaining local cooldown, and unload cancels that timer. Effective API/coordinator
health caches preserve at most 1024 target histories until the runtime resets; at
capacity new target projections are omitted rather than evicting active history.
The coordinator admits health events only for its current inventory. The switch also
exposes bounded rejection code and correlated send-to-response latency when given.

Temporary unqualified, failed, or busy confirmation reads retry after 20 seconds
within the original window. The SDK has eight automatic detection reads and sixteen
separate explicit confirmation reads, reserving capacity for corrections and the
final check. Detection exhaustion prompts fresh explicit verification. Routine SDK
session retirement or replacement suspends evidence; enabled correction resumes
through a fresh-session read, retaining the count, schedule, and expiry. Correction
disabled causes no resume reads. Bridge disconnect and entity removal end the
lineage; reconnecting the bridge does not replay it.

A pause during preparation prevents dispatch. A submitted correction is cancelled
through the bounded protocol while retaining its receipt and passive observation.
A definitive cancellation before send refunds the send budget; an attempted or
uncertain send does not. After the third correction receipt, the controller allows
15 seconds for settling before a final fresh read. Confirmed continued disagreement
raises one persistent notification and one `eufy_sdk_light_correction_failed` event;
a matching report/read suppresses that failure. An unavailable or unqualified read
does not authorize a correction or assert confirmed failure. Exhausted verification,
lost protection, or expiry during unresolved verification instead produces one
notification and event saying the light state could not be freshly verified.
`light_correction_failure_reason` distinguishes this from `confirmed_opposite`;
`verification_exhausted` is terminal for the lineage.

Corrective receipts remain separate from the explicit command receipt and do not
create an optimistic light state. Actual reported state and existing pending-accepted
presentation remain authoritative. `last_contrary_at` and `correction_window_until`
are UTC display timestamps; elapsed-time enforcement uses a monotonic clock.
SDK reports cannot identify whether an opposite change came from delayed device
work, motion lighting, or the Eufy app. Enabling correction can override those
external changes. Offline tests do not establish physical-device acceptance.

## Historical experimental.6 preparation and earlier evidence

The following retained notes describe earlier source and test runs, not current experimental.7 qualification.


## Local source changes, 2026-10-17

This local release candidate packages the reviewed source at `0f4ff70`, including siren/doorbell changes at `cad908c` and floodlight pending-success correction at `c50fe57`. Version `0.3.1-s1.experimental.6` is the integration companion for runtime `0.4.1-s1.experimental.6`. The deterministic ZIP is prepared locally; nothing is installed or published. The source-review test counts below are historical; the preparation receipts record fresh offline verification.

- Sirens require a bound `siren` descriptor with both `trigger` and `stop`, or standalone sensor-family `test` and `stop`. Missing, incomplete or malformed manifests create no new siren entities; withdrawn actions make existing entities unavailable and direct calls fail before RPC. HomeBase/camera duration commands retain the 30-second default. Command acknowledgements and elapsed durations never establish sounding state: without a reported boolean it remains unknown.
- Doorbell presses publish legacy `pressed`, then standard `ring`, with a separate write for each. Existing event types and entity identities remain. Automations filtered to either type each receive one matching event; an unfiltered state automation can run twice. Detection event types remain unchanged.
- Storage getters reuse validated UTC observation datetimes and skip diagnostics validation while disconnected. Existing freshness, provenance, in-place update validation and expiry semantics remain. Other warned push/binary/select getters already read cached scalars or fixed dictionary keys. Offline timings are microseconds; the reported 0.6-second warning is not reproduced or attributed by these checks.
- The existing HA 2026.7.0 / Python 3.14.4 environment runs all 446 pytest items through `tools/run_offline_tests.py`: 444 passed, two inherited registry-test failures, and 278 passed subtests. All 25 new items and 43 new subtests pass. The same runner against untouched `5833e03` yields 419 passed, the same two failures, and 235 passed subtests. No packages are installed. HA 2026.10 runtime behavior remains unqualified.
- Add-on compatibility is action-surface based: review.7 or published .5 bridges lacking siren descriptors/actions simply offer no new siren entities or leave existing ones unavailable. No hello/protocol version requirement is introduced. Supported cameras gain controls only when the paired SDK advertises complete supported routes; audible acceptance is separate. Existing registry records are retained, and newly gained methods require an entry reload to create entities.

The earlier candidate notes below describe retained history. See INTEGRATION_NOTICES.md for this review's modification record and SOURCE_MANIFEST.json for current local inventory.

Experimental integration `0.3.1-s1.experimental.6` is a local companion candidate for runtime `0.4.1-s1.experimental.6`. Use the exact matching published prerelease and checksums, or a separately approved verified local candidate. Candidate preparation does not establish release or HACS availability. Packaging establishes neither installation, authentication, stock-Core playback, camera switching nor physical-device acceptance. See [INSTALL.md](INSTALL.md).

The source is a composite S1/T9000 baseline, not a byte-equivalent official upstream version. The integration version identifies this experimental candidate; historical component labels do not identify official release inclusion. Existing contribution branches and upstream history remain separate. This candidate incorporates local integration development through `c50fe57` (including `cad908c`, `d762809` and `3c53503`) on top of published `0.3.1.dev2` commit `6d4402dca70c24560cc48860dd6affbae632f00d`. The config schema and existing entry/registry identifiers are retained.

## Candidate release notes: 0.3.1-s1.experimental.6

- Successful latest floodlight writes present the requested state while confirmation is pending. Fresh contrary reports or unchanged real reads during the request retain the bounded automatic correction path. The 30-second settling deadline and 8-second correction debounce remain; requested presentation does not prove physical state.
- Camera siren controls require complete supported actions, and doorbells publish the standard `ring` event alongside legacy `pressed`.

- Native Home Assistant WebRTC live view forwards offers and ICE candidates to the bridge for streaming cameras uniquely bound to a T9000 station. Only the explicit boolean `webrtcRelay: true` in the bridge WebSocket hello selects the native camera class. Relay-off, missing advertisements and other cameras retain their existing class and RTSP source; battery worker suppression below applies to both camera classes. The matching bridge requires `BRIDGE_WEBRTC_RELAY=1`; there is no separate integration switch.
- A relay advertisement change after reconnect schedules one config-entry reload to recreate camera classes. Unchanged advertisements do not reload; a fresh setup establishes a new hello baseline. Listeners and signaling sessions are cleaned up on unload/removal or transport loss.
- Bridge admission errors retain `webrtc_busy` and `webrtc_releasing`, even when an early close event precedes the open reply. No automatic retry or hidden stream start is added.
- After a successful floodlight command, a short settling guard prevents brief contrary reports from making the displayed light flicker. A contrary boolean report starts an 8-second debounce timer; agreement cancels that timer. Regardless of the remaining debounce time, the guard ends no later than 30 seconds after the command, even if refreshes fail. The normal post-write refresh remains 20 seconds. Reports still update the cache and observation receipts; the displayed intent is not physical-device confirmation. New commands and entity removal retire stale settling timers.
- Storage diagnostics distinguish off, waiting and failed attempts, retain bounded failure categories and tolerate a finite fractional next-attempt timestamp from older bridges. Unlabelled enum codes display their raw value.
- Offline discovery with the specified interpreter ran 339 unittest cases: 337 passed, with the inherited firmware-registry assertion failure and reconfigure-registry API error. Broader pytest with an external coroutine runner passed 419 test items and 235 subtests, with the same two failures. These ports add 25 unittest cases. The interpreter imports HA 2026.7.0, so HA 2026.10 API and runtime qualification remain open. These checks do not establish installation or media acceptance.
- All six local Core overlays remain excluded. Stock-Core playback, camera switching, ARM and non-S1 operation remain unqualified. This integration contains neither the paired runtime nor optional frontend installation.

## Upstream ports adapted on 2026-10-16

Battery classification and stream-worker suppression follow upstream `bde4a566ea8549543b2c3346a077f91fdc106069`; the preload checker, Repair and English issue strings follow `2ee7e77752340a87d54064ecc4b923165e15ddf0`. Explicit `power` of `battery` or `wired` wins. Older bridges fall back to battery capability plus a battery level that is not `None`; zero counts. A missing level can miss a battery device, and a wired sentinel level can be misclassified until accurate explicit metadata is paired.

The base camera returns no HA stream worker for battery cameras. STREAM, RTSP source, snapshots, native T9000 selection and signaling remain unchanged. Battery cameras intentionally lose HA HLS fallback and `camera.record`. Native WebRTC remains available where the existing relay gate selects it; this port does not qualify live playback. A fixable, nonpersistent warning offers to disable battery-camera preloading. Only submitting its confirmation changes that camera preference; manual changes clear the warning on the next entity addition, not through continuous preference monitoring.

Narrow missing-data alarm/arming guards adapt upstream `3d4dd6b2e42a7150f1a7f1a4541cd7c6ca9c5702` while preserving existing property/setup guards, valued current-mode events, observation receipts and reported-mode semantics. Floodlight settling and native WebRTC implementation files remain unchanged. Original MIT copyright and permission notices are retained; see INTEGRATION_NOTICES.md and SOURCE_MANIFEST.json.

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


## Historical input documentation and modification notices

# Experimental integration artifact

## Local source changes, 2026-10-17

This local release candidate packages the reviewed source at `0f4ff70`, including siren/doorbell changes at `cad908c` and floodlight pending-success correction at `c50fe57`. Version `0.3.1-s1.experimental.6` is the integration companion for runtime `0.4.1-s1.experimental.6`. The deterministic ZIP is prepared locally; nothing is installed or published. The source-review test counts below are historical; the preparation receipts record fresh offline verification.

- Sirens require a bound `siren` descriptor with both `trigger` and `stop`, or standalone sensor-family `test` and `stop`. Missing, incomplete or malformed manifests create no new siren entities; withdrawn actions make existing entities unavailable and direct calls fail before RPC. HomeBase/camera duration commands retain the 30-second default. Command acknowledgements and elapsed durations never establish sounding state: without a reported boolean it remains unknown.
- Doorbell presses publish legacy `pressed`, then standard `ring`, with a separate write for each. Existing event types and entity identities remain. Automations filtered to either type each receive one matching event; an unfiltered state automation can run twice. Detection event types remain unchanged.
- Storage getters reuse validated UTC observation datetimes and skip diagnostics validation while disconnected. Existing freshness, provenance, in-place update validation and expiry semantics remain. Other warned push/binary/select getters already read cached scalars or fixed dictionary keys. Offline timings are microseconds; the reported 0.6-second warning is not reproduced or attributed by these checks.
- The existing HA 2026.7.0 / Python 3.14.4 environment runs all 446 pytest items through `tools/run_offline_tests.py`: 444 passed, two inherited registry-test failures, and 278 passed subtests. All 25 new items and 43 new subtests pass. The same runner against untouched `5833e03` yields 419 passed, the same two failures, and 235 passed subtests. No packages are installed. HA 2026.10 runtime behavior remains unqualified.
- Add-on compatibility is action-surface based: review.7 or published .5 bridges lacking siren descriptors/actions simply offer no new siren entities or leave existing ones unavailable. No hello/protocol version requirement is introduced. Supported cameras gain controls only when the paired SDK advertises complete supported routes; audible acceptance is separate. Existing registry records are retained, and newly gained methods require an entry reload to create entities.

The earlier candidate notes below describe retained history. See INTEGRATION_NOTICES.md for this review's modification record and SOURCE_MANIFEST.json for current local inventory.

Experimental integration `0.3.1-s1.experimental.6` is a local companion candidate for runtime `0.4.1-s1.experimental.6`. Use the exact matching published prerelease and checksums, or a separately approved verified local candidate. Candidate preparation does not establish release or HACS availability. Packaging establishes neither installation, authentication, stock-Core playback, camera switching nor physical-device acceptance. See [INSTALL.md](INSTALL.md).

The source is a composite S1/T9000 baseline, not a byte-equivalent official upstream version. The integration version identifies this experimental candidate; historical component labels do not identify official release inclusion. Existing contribution branches and upstream history remain separate. This candidate incorporates local integration development through `c50fe57` (including `cad908c`, `d762809` and `3c53503`) on top of published `0.3.1.dev2` commit `6d4402dca70c24560cc48860dd6affbae632f00d`. The config schema and existing entry/registry identifiers are retained.

## Candidate release notes: 0.3.1-s1.experimental.6

- Successful latest floodlight writes present the requested state while confirmation is pending. Fresh contrary reports or unchanged real reads during the request retain the bounded automatic correction path. The 30-second settling deadline and 8-second correction debounce remain; requested presentation does not prove physical state.
- Camera siren controls require complete supported actions, and doorbells publish the standard `ring` event alongside legacy `pressed`.

- Native Home Assistant WebRTC live view forwards offers and ICE candidates to the bridge for streaming cameras uniquely bound to a T9000 station. Only the explicit boolean `webrtcRelay: true` in the bridge WebSocket hello selects the native camera class. Relay-off, missing advertisements and other cameras retain their existing class and RTSP source; battery worker suppression below applies to both camera classes. The matching bridge requires `BRIDGE_WEBRTC_RELAY=1`; there is no separate integration switch.
- A relay advertisement change after reconnect schedules one config-entry reload to recreate camera classes. Unchanged advertisements do not reload; a fresh setup establishes a new hello baseline. Listeners and signaling sessions are cleaned up on unload/removal or transport loss.
- Bridge admission errors retain `webrtc_busy` and `webrtc_releasing`, even when an early close event precedes the open reply. No automatic retry or hidden stream start is added.
- After a successful floodlight command, a short settling guard prevents brief contrary reports from making the displayed light flicker. A contrary boolean report starts an 8-second debounce timer; agreement cancels that timer. Regardless of the remaining debounce time, the guard ends no later than 30 seconds after the command, even if refreshes fail. The normal post-write refresh remains 20 seconds. Reports still update the cache and observation receipts; the displayed intent is not physical-device confirmation. New commands and entity removal retire stale settling timers.
- Storage diagnostics distinguish off, waiting and failed attempts, retain bounded failure categories and tolerate a finite fractional next-attempt timestamp from older bridges. Unlabelled enum codes display their raw value.
- Offline discovery with the specified interpreter ran 339 unittest cases: 337 passed, with the inherited firmware-registry assertion failure and reconfigure-registry API error. Broader pytest with an external coroutine runner passed 419 test items and 235 subtests, with the same two failures. These ports add 25 unittest cases. The interpreter imports HA 2026.7.0, so HA 2026.10 API and runtime qualification remain open. These checks do not establish installation or media acceptance.
- All six local Core overlays remain excluded. Stock-Core playback, camera switching, ARM and non-S1 operation remain unqualified. This integration contains neither the paired runtime nor optional frontend installation.

## Upstream ports adapted on 2026-10-16

Battery classification and stream-worker suppression follow upstream `bde4a566ea8549543b2c3346a077f91fdc106069`; the preload checker, Repair and English issue strings follow `2ee7e77752340a87d54064ecc4b923165e15ddf0`. Explicit `power` of `battery` or `wired` wins. Older bridges fall back to battery capability plus a battery level that is not `None`; zero counts. A missing level can miss a battery device, and a wired sentinel level can be misclassified until accurate explicit metadata is paired.

The base camera returns no HA stream worker for battery cameras. STREAM, RTSP source, snapshots, native T9000 selection and signaling remain unchanged. Battery cameras intentionally lose HA HLS fallback and `camera.record`. Native WebRTC remains available where the existing relay gate selects it; this port does not qualify live playback. A fixable, nonpersistent warning offers to disable battery-camera preloading. Only submitting its confirmation changes that camera preference; manual changes clear the warning on the next entity addition, not through continuous preference monitoring.

Narrow missing-data alarm/arming guards adapt upstream `3d4dd6b2e42a7150f1a7f1a4541cd7c6ca9c5702` while preserving existing property/setup guards, valued current-mode events, observation receipts and reported-mode semantics. Floodlight settling and native WebRTC implementation files remain unchanged. Original MIT copyright and permission notices are retained; see INTEGRATION_NOTICES.md and SOURCE_MANIFEST.json.

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

## Optional contrary light correction

The integration options include **Correct confirmed contrary lights to OFF only**
(`light_correction_enabled`, default off) and an optional **Pause light corrections
helper** (`light_correction_pause_entity`, no helper by default). The pause selector
accepts an `input_boolean` or `switch`. A selected helper pauses corrections while
on, unavailable, or unknown; off resumes eligible work. Observation events and
attributes remain available with automatic correction disabled.

This requires a bridge advertising `lateContraryEffects` version 1, a 600000 ms
watch, confirmation support, and bounded state writes. An older bridge keeps its
existing write behavior. Only a new explicit HA light command establishes correction
authority, including commands issued by HA automations. A newer command immediately
replaces the previous lineage. Automatic correction supports OFF intent only.
ON recovery uses the original command and existing bounded SDK retry; ON
observations remain available with an `on_observation_only` suppression reason.

The latest explicit command owns one monotonic ten-minute window. A fresh SDK
confirmation of contrary state schedules corrections at 15, 60, and 300 seconds from
the first confirmation. Each send requires a fresh qualified SDK read, and sends
are at least 45 seconds apart, including overdue work after a pause. Corrections
retain the original window and maximum three-send budget. A reported match cancels
pending work while retaining the watch for a later reversal. The original expiry
travels with each correction and bounds queued transport sends. Ordinary SDK
recovery must settle before a fresh read and correction; correction-origin writes
cannot create an additional SDK recovery child.

SDK `commandHealth` receipts, events, and device snapshots share one per-light
command gate. `command_degraded` cancels pending automatic corrections while
retaining passive observation. Explicit HA commands still receive one normal SDK
attempt; the SDK suppresses automatic retry while degraded. A healthy receipt,
health event, or bounded cooldown expiry allows eligible OFF work to resume within
its original window and remaining budget. `command_health` describes the command
path, not device availability or confirmed physical light state. Optional SDK health
revisions order receipts, events, and snapshots; older or subsequently unversioned
projections cannot clear a newer gate. Equal revisions cannot extend cooldown.
Disconnect/ready resets ordering for a replacement SDK runtime while retaining only
the remaining local cooldown, and unload cancels that timer. Effective API/coordinator
health caches preserve at most 1024 target histories until the runtime resets; at
capacity new target projections are omitted rather than evicting active history.
The coordinator admits health events only for its current inventory. The switch also
exposes bounded rejection code and correlated send-to-response latency when given.

Temporary unqualified, failed, or busy confirmation reads retry after 20 seconds
within the original window. The SDK has eight automatic detection reads and sixteen
separate explicit confirmation reads, reserving capacity for corrections and the
final check. Detection exhaustion prompts fresh explicit verification. Routine SDK
session retirement or replacement suspends evidence; enabled correction resumes
through a fresh-session read, retaining the count, schedule, and expiry. Correction
disabled causes no resume reads. Bridge disconnect and entity removal end the
lineage; reconnecting the bridge does not replay it.

A pause during preparation prevents dispatch. A submitted correction is cancelled
through the bounded protocol while retaining its receipt and passive observation.
A definitive cancellation before send refunds the send budget; an attempted or
uncertain send does not. After the third correction receipt, the controller allows
15 seconds for settling before a final fresh read. Confirmed continued disagreement
raises one persistent notification and one `eufy_sdk_light_correction_failed` event;
a matching report/read suppresses that failure. An unavailable or unqualified read
does not authorize a correction or assert confirmed failure. Exhausted verification,
lost protection, or expiry during unresolved verification instead produces one
notification and event saying the light state could not be freshly verified.
`light_correction_failure_reason` distinguishes this from `confirmed_opposite`;
`verification_exhausted` is terminal for the lineage.

Corrective receipts remain separate from the explicit command receipt and do not
create an optimistic light state. Actual reported state and existing pending-accepted
presentation remain authoritative. `last_contrary_at` and `correction_window_until`
are UTC display timestamps; elapsed-time enforcement uses a monotonic clock.
SDK reports cannot identify whether an opposite change came from delayed device
work, motion lighting, or the Eufy app. Enabling correction can override those
external changes. Offline tests do not establish physical-device acceptance.
