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

# Integration installation, migration and rollback

**Experimental integration `0.3.1-s1.experimental.7` requires a separate installation decision.** Use the exact matching published prerelease and checksums, or a separately approved verified local candidate ZIP and hashes. This local experimental.7 candidate is unpublished; release and HACS availability are not established. These instructions do not authorize installation, authentication or HA restart. The earlier source preview alone is not this artifact. Display name: **Eufy SDK S1 Experimental (genomez)**; domain: `eufy_sdk`; owner: `@genomez`; repository: `https://github.com/genomez/ha-eufy-sdk`.

Confirm the separately reviewed paired bridge/add-on artifact and its recovery plan first. This archive includes no bridge, account session, add-on data, Core overlay or dashboard/player installation. [CORE_COMPATIBILITY.md](CORE_COMPATIBILITY.md) documents unqualified stock-Core media and switching.

## Backup and identity checkpoint

1. Record privately the exact current component owner: HACS repository URL and selected release/commit, or manual source. Record its manifest version, complete file list and SHA-256 hashes; the `eufy_sdk` config entry ID/version, exact host/port, unique ID and options; entity IDs/unique IDs, device identifiers and registry relationships. Compare the candidate against the exact installed component. A shared domain is not migration proof.
2. Create and download a completed Home Assistant backup including its configuration. Verify readability and preserve its emergency kit/encryption key as applicable. Keep an exact copy of the prior `custom_components/eufy_sdk` directory and file hashes outside `custom_components` and outside repositories. A version number or remote release link is not a rollback copy.
3. Preserve coherent configuration/registry state together: `.storage/core.config_entries`, `.storage/core.entity_registry`, `.storage/core.device_registry`, `.storage/core.area_registry`, applicable floor/label registries, all HACS storage/ownership files, and related dashboards/automations/YAML. Use the verified HA backup or a consistent copy while HA is stopped in an approved maintenance window. Live individual file copies can be inconsistent. Preserve the whole configuration when unsure what is relevant. Do not selectively edit/reconstruct registry files.
4. Preserve paired bridge/add-on repository/slug, exact image digest/version, endpoint and private data through its own recovery procedure. Verify the proposed endpoint serves the same intended account/device installation using existing private baseline evidence. Reconfigure validation checks bridge connection/auth state, **not account or device identity**. Stop if equivalence is unknown or the Core environment will change. Do not run concurrent old/new bridges for the same account as a test.
5. Select **one** file owner: this fork in HACS or a manual installation. Preserve the HA config entry; HACS repository ownership and HA config-entry ownership are separate. Never remove an HA integration/config entry as a way to replace files.

Record backup identifiers/hashes privately. Backups may contain credentials and private data; never upload them to the fork, ZIP, build context or issue tracker. Verify recovery before changing ownership/files.

## Config entries and endpoint migration

The config schema remains version 1. `host` and `port` are stored in entry data; the endpoint unique ID is `host:port`. There is no schema migration hook. Reauthentication reuses the stored endpoint. Options change poll/SOC-refresh intervals; **Reconfigure** changes host/port on the existing entry.

For a compatible existing entry, retain its exact endpoint until the new paired bridge is ready. After the identity checkpoint above, use the existing entry's **Reconfigure** menu to enter the new bridge host/port. Connection/authentication must succeed before host/port, endpoint unique ID and title update. Entry ID, unrelated entry data, options and registry identifiers are preserved. An endpoint already owned by another entry is rejected without mutation. No additional entry is created. Challenge answers are explicit; no implicit login retrigger, device query or camera start validates the endpoint. Pending/blocked/cooldown/auth failure leaves the original entry intact.

Changing a hostname can point at a different account, and aliases can describe the same bridge with different strings. Duplicate checks compare endpoint strings; they cannot detect aliases or prove account/device equivalence. If the new endpoint is ambiguous, stop at the identity checkpoint. Do not manually edit `.storage`, delete/recreate entries, add a second entry to work around failure, or change the domain.

## Manual path

1. After a separate installation decision, obtain the exact reviewed `eufy-sdk-s1-0.3.1-s1.experimental.7.zip` and SHA-256 receipt from the matching published prerelease, or use a separately approved verified local candidate. Verify the hash and extract into a separate staging directory. The ZIP contains root documentation and exactly one runtime subtree: `custom_components/eufy_sdk/`.
2. If HACS currently manages the component, preserve its owner record and recovery first. During approved maintenance, remove the old repository's management using HACS controls. Inspect whether the UI will remove files before confirming. Keep the HA config entry. Confirm HACS no longer manages this domain; if unclear, stop instead of editing HACS storage.
3. With HA stopped in the approved maintenance window, stage the exact old component directory into retained private recovery storage after checking the target path. Install the complete candidate `custom_components/eufy_sdk` directory into the HA configuration directory's `custom_components`. Replace the entire component instead of merging files. Copy no repository root files, tests or frontend assets into the component. Ensure one runtime directory exists.
4. Compare installed filenames and hashes with the package receipt, including component license/manifest. Start HA and verify loaded version `0.3.1-s1.experimental.7`, existing entry/endpoint and expected entity/device identifiers. Do not add an entry for a replacement install. If needed, use the endpoint reconfigure flow only after the identity checkpoint.

For a genuinely new install with no prior entries/domain owner, after the separately approved paired bridge is ready, add **Eufy SDK S1 Experimental (genomez)** in Settings > Devices & services, enter the reachable bridge endpoint, and complete only prompted authentication challenges. New setup is distinct from migration.

## HACS path: exact matching published prerelease required

The following path requires an exact matching full GitHub prerelease **`0.3.1-s1.experimental.7`** and a separate installation decision. Candidate preparation does not establish that a release exists or can be selected in HACS. The inherited default branch is different code. Retained metadata sets `hide_default_branch: true`. Verify the exact published source and checksums; a tag alone is insufficient. HACS prerelease update tracking is opt-in; older HACS versions may also hide prereleases until **Show beta versions** is enabled. If that release is absent or cannot be selected, stop; do not select the default branch/source preview.

1. Complete the backup and identity checkpoint. If another HACS repository owns `eufy_sdk`, remove its management through HACS after preserving exact files/owner record. Inspect removal consequences; retain the HA config entry. For manual-to-HACS conversion, preserve/stage the manual files away during approved maintenance so HACS becomes the sole file owner.
2. In HACS menu > Custom repositories, add `https://github.com/genomez/ha-eufy-sdk`, type **Integration**. Open this repository's **Download/Redownload** dialog, expand **Need a different version?**, and choose **Release `0.3.1-s1.experimental.7`** (marked pre-release). Verify that the dialog says version `0.3.1-s1.experimental.7` will be downloaded and displays the intended repository/owner. Older HACS versions may require **Show beta versions** first. If the exact version cannot be selected, stop. Do not select an inherited stable release or the default branch. This candidate uses normal `custom_components/eufy_sdk` repository layout, not `zip_release`; the manual ZIP is separate.
3. Verify the fork is sole HACS owner, compare downloaded component files to candidate hashes, then restart HA in the approved window. Keep the existing HA entry. Check version/endpoint and entity/device identity before further acceptance. Reconfigure the endpoint only after the identity checkpoint if required.
4. Stop if HACS rejects ownership, removal appears to delete an HA entry, or version selection is uncertain. Do not force duplicate repositories, edit `.storage/hacs*`, or redownload old files over the candidate.

For future prerelease update checks on a downloaded repository, current HACS supplies a disabled-by-default **Pre-release** switch entity: enable that entity and turn it on only if beta updates are wanted. This is separate from explicitly selecting the release for installation. HACS installation and the fork replacement controls have not been exercised by packaging. Concrete UI ownership review remains required; metadata alone is not acceptance.

## Acceptance boundary

Check setup/load, endpoint and retained identifiers through bounded observations. Do not start/switch cameras, operate lights, trigger authentication beyond explicit approved challenges or change automations without separate approval. Stock-Core playback and camera switching remain unqualified after successful setup. Setup failure, changed IDs, duplicate entries, unexpected auth or physical-state disagreement requires stopping and considering rollback.

## Exact rollback

1. Stop candidate changes, save failure evidence privately, and record changed files/entry data/HACS ownership. Obtain the maintenance window before stopping HA or restoring configuration. Use recovery captured immediately before replacement, not an inherited upstream version label.
2. If only the endpoint changed and the prior bridge is already reachable with its intended authentication state, use **Reconfigure while the candidate is still installed and HA is running** to return the existing entry to its original host/port. Verify original entry ID/schema, data/options, unique ID/title and registry identifiers against the private baseline. This supported alternative avoids a full configuration restore only when the original state is recovered exactly. A custom original title or other config/HACS/registry changes may still require restoration. If the old endpoint is unavailable or baseline comparison fails, use the coherent backup path in step 4 instead.
3. With HA stopped in the approved window, stage the candidate outside `custom_components` into retained recovery storage. Restore the entire exact prior component, including license. Verify original filenames/hashes and absence of candidate-only runtime files; do not merge trees.
4. Restore the prior sole HACS/manual owner through supported controls. If reconfigure-back did not recover the exact original configuration, or config/registry/HACS state changed beyond that endpoint, restore the **matching configuration snapshot as a unit** through documented HA backup restoration. This overwrites other changes since capture; review that impact before authorization. Do not merge registry records, reset unique IDs or delete entries. If configuration was unchanged or recovered exactly in step 2, verify original records and keep them intact.
5. Coordinate paired bridge/add-on restoration to exact prior identity/digest/data and endpoint through its own recovery path if it changed. Restore Core only through its separately approved recovery; the integration ZIP cannot restore add-on data or overlays.
6. Start HA and verify prior version/hashes, original entry ID/schema/host/port/unique ID, sole file owner and original entity/device IDs against the baseline. Successful process start is insufficient. Retain pre-install and failed-candidate recovery material until owner acceptance; no automated cleanup or deletion is supplied.

## Documentation basis

Reviewed 2026-10-14. Replacement checkpoints are this fork's policy; these primary sources do not certify the candidate/migration.

- [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/): one component under `custom_components` and required manifest fields.
- [HACS manifest/version semantics](https://www.hacs.xyz/docs/publish/start/): display name, default-branch hiding, minimum fields and releases rather than tags for version selection.
- [HACS release download dialog](https://github.com/hacs/frontend/blob/main/src/components/dialogs/hacs-download-dialog.ts), [release-list endpoint](https://github.com/hacs/integration/blob/main/custom_components/hacs/websocket/repository.py), and [beta update switch](https://www.hacs.dev/docs/use/entities/switch/): explicit release selection and opt-in update tracking are separate; source review is not UI acceptance.
- [HACS custom repositories](https://www.hacs.xyz/docs/faq/custom_repositories/), [component download location](https://www.hacs.xyz/docs/use/repositories/type/integration/).
- [HA manifest domain/version](https://developers.home-assistant.io/docs/creating_integration_manifest/): unique unchanged domain matching directory, valid custom version.
- [HA config-entry migration/reconfigure](https://developers.home-assistant.io/docs/core/integration/config_flow/): reconfigure updates existing entries rather than creating more.
- [HA backup/download/restore](https://www.home-assistant.io/common-tasks/general/#backups): preserve configuration/recovery keys; restore overwrites selected parts.


## Historical input documentation and modification notices

# Integration installation, migration and rollback

**Experimental integration `0.3.1-s1.experimental.6` requires a separate installation decision.** Use the exact matching published prerelease and checksums, or a separately approved verified local candidate ZIP and hashes. Candidate preparation does not establish release or HACS availability. These instructions do not authorize installation, authentication or HA restart. The earlier source preview alone is not this artifact. Display name: **Eufy SDK S1 Experimental (genomez)**; domain: `eufy_sdk`; owner: `@genomez`; repository: `https://github.com/genomez/ha-eufy-sdk`.

Confirm the separately reviewed paired bridge/add-on artifact and its recovery plan first. This archive includes no bridge, account session, add-on data, Core overlay or dashboard/player installation. [CORE_COMPATIBILITY.md](CORE_COMPATIBILITY.md) documents unqualified stock-Core media and switching.

## Backup and identity checkpoint

1. Record privately the exact current component owner: HACS repository URL and selected release/commit, or manual source. Record its manifest version, complete file list and SHA-256 hashes; the `eufy_sdk` config entry ID/version, exact host/port, unique ID and options; entity IDs/unique IDs, device identifiers and registry relationships. Compare the candidate against the exact installed component. A shared domain is not migration proof.
2. Create and download a completed Home Assistant backup including its configuration. Verify readability and preserve its emergency kit/encryption key as applicable. Keep an exact copy of the prior `custom_components/eufy_sdk` directory and file hashes outside `custom_components` and outside repositories. A version number or remote release link is not a rollback copy.
3. Preserve coherent configuration/registry state together: `.storage/core.config_entries`, `.storage/core.entity_registry`, `.storage/core.device_registry`, `.storage/core.area_registry`, applicable floor/label registries, all HACS storage/ownership files, and related dashboards/automations/YAML. Use the verified HA backup or a consistent copy while HA is stopped in an approved maintenance window. Live individual file copies can be inconsistent. Preserve the whole configuration when unsure what is relevant. Do not selectively edit/reconstruct registry files.
4. Preserve paired bridge/add-on repository/slug, exact image digest/version, endpoint and private data through its own recovery procedure. Verify the proposed endpoint serves the same intended account/device installation using existing private baseline evidence. Reconfigure validation checks bridge connection/auth state, **not account or device identity**. Stop if equivalence is unknown or the Core environment will change. Do not run concurrent old/new bridges for the same account as a test.
5. Select **one** file owner: this fork in HACS or a manual installation. Preserve the HA config entry; HACS repository ownership and HA config-entry ownership are separate. Never remove an HA integration/config entry as a way to replace files.

Record backup identifiers/hashes privately. Backups may contain credentials and private data; never upload them to the fork, ZIP, build context or issue tracker. Verify recovery before changing ownership/files.

## Config entries and endpoint migration

The config schema remains version 1. `host` and `port` are stored in entry data; the endpoint unique ID is `host:port`. There is no schema migration hook. Reauthentication reuses the stored endpoint. Options change poll/SOC-refresh intervals; **Reconfigure** changes host/port on the existing entry.

For a compatible existing entry, retain its exact endpoint until the new paired bridge is ready. After the identity checkpoint above, use the existing entry's **Reconfigure** menu to enter the new bridge host/port. Connection/authentication must succeed before host/port, endpoint unique ID and title update. Entry ID, unrelated entry data, options and registry identifiers are preserved. An endpoint already owned by another entry is rejected without mutation. No additional entry is created. Challenge answers are explicit; no implicit login retrigger, device query or camera start validates the endpoint. Pending/blocked/cooldown/auth failure leaves the original entry intact.

Changing a hostname can point at a different account, and aliases can describe the same bridge with different strings. Duplicate checks compare endpoint strings; they cannot detect aliases or prove account/device equivalence. If the new endpoint is ambiguous, stop at the identity checkpoint. Do not manually edit `.storage`, delete/recreate entries, add a second entry to work around failure, or change the domain.

## Manual path

1. After a separate installation decision, obtain the exact reviewed `eufy-sdk-s1-0.3.1-s1.experimental.6.zip` and SHA-256 receipt from the matching published prerelease, or use a separately approved verified local candidate. Verify the hash and extract into a separate staging directory. The ZIP contains root documentation and exactly one runtime subtree: `custom_components/eufy_sdk/`.
2. If HACS currently manages the component, preserve its owner record and recovery first. During approved maintenance, remove the old repository's management using HACS controls. Inspect whether the UI will remove files before confirming. Keep the HA config entry. Confirm HACS no longer manages this domain; if unclear, stop instead of editing HACS storage.
3. With HA stopped in the approved maintenance window, stage the exact old component directory into retained private recovery storage after checking the target path. Install the complete candidate `custom_components/eufy_sdk` directory into the HA configuration directory's `custom_components`. Replace the entire component instead of merging files. Copy no repository root files, tests or frontend assets into the component. Ensure one runtime directory exists.
4. Compare installed filenames and hashes with the package receipt, including component license/manifest. Start HA and verify loaded version `0.3.1-s1.experimental.6`, existing entry/endpoint and expected entity/device identifiers. Do not add an entry for a replacement install. If needed, use the endpoint reconfigure flow only after the identity checkpoint.

For a genuinely new install with no prior entries/domain owner, after the separately approved paired bridge is ready, add **Eufy SDK S1 Experimental (genomez)** in Settings > Devices & services, enter the reachable bridge endpoint, and complete only prompted authentication challenges. New setup is distinct from migration.

## HACS path: exact matching published prerelease required

The following path requires an exact matching full GitHub prerelease **`0.3.1-s1.experimental.6`** and a separate installation decision. Candidate preparation does not establish that a release exists or can be selected in HACS. The inherited default branch is different code. Retained metadata sets `hide_default_branch: true`. Verify the exact published source and checksums; a tag alone is insufficient. HACS prerelease update tracking is opt-in; older HACS versions may also hide prereleases until **Show beta versions** is enabled. If that release is absent or cannot be selected, stop; do not select the default branch/source preview.

1. Complete the backup and identity checkpoint. If another HACS repository owns `eufy_sdk`, remove its management through HACS after preserving exact files/owner record. Inspect removal consequences; retain the HA config entry. For manual-to-HACS conversion, preserve/stage the manual files away during approved maintenance so HACS becomes the sole file owner.
2. In HACS menu > Custom repositories, add `https://github.com/genomez/ha-eufy-sdk`, type **Integration**. Open this repository's **Download/Redownload** dialog, expand **Need a different version?**, and choose **Release `0.3.1-s1.experimental.6`** (marked pre-release). Verify that the dialog says version `0.3.1-s1.experimental.6` will be downloaded and displays the intended repository/owner. Older HACS versions may require **Show beta versions** first. If the exact version cannot be selected, stop. Do not select an inherited stable release or the default branch. This candidate uses normal `custom_components/eufy_sdk` repository layout, not `zip_release`; the manual ZIP is separate.
3. Verify the fork is sole HACS owner, compare downloaded component files to candidate hashes, then restart HA in the approved window. Keep the existing HA entry. Check version/endpoint and entity/device identity before further acceptance. Reconfigure the endpoint only after the identity checkpoint if required.
4. Stop if HACS rejects ownership, removal appears to delete an HA entry, or version selection is uncertain. Do not force duplicate repositories, edit `.storage/hacs*`, or redownload old files over the candidate.

For future prerelease update checks on a downloaded repository, current HACS supplies a disabled-by-default **Pre-release** switch entity: enable that entity and turn it on only if beta updates are wanted. This is separate from explicitly selecting the release for installation. HACS installation and the fork replacement controls have not been exercised by packaging. Concrete UI ownership review remains required; metadata alone is not acceptance.

## Acceptance boundary

Check setup/load, endpoint and retained identifiers through bounded observations. Do not start/switch cameras, operate lights, trigger authentication beyond explicit approved challenges or change automations without separate approval. Stock-Core playback and camera switching remain unqualified after successful setup. Setup failure, changed IDs, duplicate entries, unexpected auth or physical-state disagreement requires stopping and considering rollback.

## Exact rollback

1. Stop candidate changes, save failure evidence privately, and record changed files/entry data/HACS ownership. Obtain the maintenance window before stopping HA or restoring configuration. Use recovery captured immediately before replacement, not an inherited upstream version label.
2. If only the endpoint changed and the prior bridge is already reachable with its intended authentication state, use **Reconfigure while the candidate is still installed and HA is running** to return the existing entry to its original host/port. Verify original entry ID/schema, data/options, unique ID/title and registry identifiers against the private baseline. This supported alternative avoids a full configuration restore only when the original state is recovered exactly. A custom original title or other config/HACS/registry changes may still require restoration. If the old endpoint is unavailable or baseline comparison fails, use the coherent backup path in step 4 instead.
3. With HA stopped in the approved window, stage the candidate outside `custom_components` into retained recovery storage. Restore the entire exact prior component, including license. Verify original filenames/hashes and absence of candidate-only runtime files; do not merge trees.
4. Restore the prior sole HACS/manual owner through supported controls. If reconfigure-back did not recover the exact original configuration, or config/registry/HACS state changed beyond that endpoint, restore the **matching configuration snapshot as a unit** through documented HA backup restoration. This overwrites other changes since capture; review that impact before authorization. Do not merge registry records, reset unique IDs or delete entries. If configuration was unchanged or recovered exactly in step 2, verify original records and keep them intact.
5. Coordinate paired bridge/add-on restoration to exact prior identity/digest/data and endpoint through its own recovery path if it changed. Restore Core only through its separately approved recovery; the integration ZIP cannot restore add-on data or overlays.
6. Start HA and verify prior version/hashes, original entry ID/schema/host/port/unique ID, sole file owner and original entity/device IDs against the baseline. Successful process start is insufficient. Retain pre-install and failed-candidate recovery material until owner acceptance; no automated cleanup or deletion is supplied.

## Documentation basis

Reviewed 2026-10-14. Replacement checkpoints are this fork's policy; these primary sources do not certify the candidate/migration.

- [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/): one component under `custom_components` and required manifest fields.
- [HACS manifest/version semantics](https://www.hacs.xyz/docs/publish/start/): display name, default-branch hiding, minimum fields and releases rather than tags for version selection.
- [HACS release download dialog](https://github.com/hacs/frontend/blob/main/src/components/dialogs/hacs-download-dialog.ts), [release-list endpoint](https://github.com/hacs/integration/blob/main/custom_components/hacs/websocket/repository.py), and [beta update switch](https://www.hacs.dev/docs/use/entities/switch/): explicit release selection and opt-in update tracking are separate; source review is not UI acceptance.
- [HACS custom repositories](https://www.hacs.xyz/docs/faq/custom_repositories/), [component download location](https://www.hacs.xyz/docs/use/repositories/type/integration/).
- [HA manifest domain/version](https://developers.home-assistant.io/docs/creating_integration_manifest/): unique unchanged domain matching directory, valid custom version.
- [HA config-entry migration/reconfigure](https://developers.home-assistant.io/docs/core/integration/config_flow/): reconfigure updates existing entries rather than creating more.
- [HA backup/download/restore](https://www.home-assistant.io/common-tasks/general/#backups): preserve configuration/recovery keys; restore overwrites selected parts.
