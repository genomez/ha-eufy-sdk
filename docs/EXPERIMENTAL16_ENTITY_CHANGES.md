# Experimental.16 entity changes for HomeBase S1

Local source candidate, compared with the experimental.15 release branches. This is a synthetic capability inventory, not a read of the owner's devices or HA registry. Actual reported parameters, vendor device type and the SDK's installed setter determine each entity. Non-T9000 stations retain their existing routes and presentation.

## Identity rules

Existing property unique IDs remain `<device identifier>_<SDK property name>`. No surviving control is renamed or given a new unique ID. Thus owner-customized entity IDs keep working. A domain change cannot retain an entity ID in its old domain: the old entry is reversibly disabled by the integration, preserving its identity and user preferences; a new read-only entity is created. Existing user disables and migration opt-outs are preserved. Previously integration-disabled floodlight controls are released when the same property becomes writable again.

## Model checklist

| T9000-attached model | Added or restored | Became read-only | Removed or omitted | Renamed |
| --- | --- | --- | --- | --- |
| T8425 floodlight | `number.*_brightness`, `switch.*_spotlight_enabled`; `switch.*_motion_light_enabled`; numbers for `motionLightDuration`, `motionLightBrightness`, `motionLightMode`; schedule sensor | None for its momentary `light` switch | No free-text schedule entity; unsupported/readless settings omitted | None |
| T8416 S350 PTZ | Recording-quality options updated on the existing select when reported | Any retained read whose SDK setter is absent | No physical spotlight entities; no privacy control without a read | None |
| T8170 family | Existing supported camera/audio/recording controls retained; recording-quality labels updated | `light`: switch to binary sensor when the reported family uses unsupported int-string (e.g. outdoor PT type 48); other family routes follow SDK flags | Privacy setter absent; no synthetic privacy sensor | None |
| T8214 doorbell | One button per quick response; refresh-list button; recording-quality labels updated | Any retained read whose setter is absent | No privacy control without a read | None |
| T8134 | Recording-quality labels updated; writable settings retained | `light`: switch to binary sensor for Cam3 type 19 | Privacy setter absent | None |
| T8171 | Recording-quality labels updated; writable settings retained | Only if an actual read exists without its setter | Physical spotlight remains omitted by SDK detection; privacy absent | None |
| T8426 floodlight | Same seven additions/restorations as T8425 | SDK determines momentary-light writability; the tested floodlight route remains writable | No free-text schedule entity; privacy absent | None |

The fixture `tests/fixtures/t9000_settings_manifests.json` records the .15/.16 schemas and exact generic-platform differences for all seven models. It uses SDK .15 `18b34ff` and .16 `c14bf36`, with the .15 bridge's scalar hiding applied. Test device types: 47, 104, 48, 93, 19, 48, 87 respectively. Supplied parameters are synthetic capability evidence, not proof that every model reports every test parameter. Quick-response additions are dynamic and recorded separately from generic properties.

## Retained writable controls and values

When described writable by the SDK, the existing switch/number/select unique IDs remain unchanged: `enabled`, `statusLed`, `motionDetection`, `imageFlipped`, `watermark`, `microphone`, `speaker`, `speakerVolume`, `audioRecording`, `recordAutoStop`, `recordDuration`, `recordInterval`, `brightness`, and `spotlightEnabled`. Doorbells retain `wdrSwitch`, `chimeSwitch`, `mechanicalChimeSwitch`, `videoQuality`, `ringtoneVolume`, `dingdongVolume`, and `dingdongRingtone` where installed. Missing boolean setters give binary sensors; missing numeric/enum setters give sensors. Readless settings are omitted. Legacy privacy entries are retired on T9000 because the SDK removes its write-only setter and supplies no privacy reading.

Recording quality uses the existing `recordingQuality` identity with `720P` = 1, `1080P` = 2, `3K` = 3. Previously shown labels were `HD (720P)`, `Full HD (1080P)`, `Max`. Automations selecting labels must use the new labels. Active-mode reads may remain unchanged after a write to mode 0. 720P has no hardware acceptance. The bridge's old brightness/spotlight/color-temperature block is removed; color temperature is only offered if the SDK actually supplies a property. No new color-temperature control is invented for white-only floodlights.

## Schedule and quick-response actions

`lightSchedule` is a sensor: state is entry count, `entries` is the sanitized list. Location and identity fields are omitted recursively; the full surrounding config is never an attribute. No text editor entity is created. Call `eufy_sdk.set_light_schedule` with an HA `device_id` and `entries` list; `[]` clears it. The SDK validates the reported backing config and preserves its other fields. No entry schema or mode labels are inferred. Motion-light duration and brightness use SDK bounds; mode is an integer number with no invented labels.

Quick-response unique IDs are `<device identifier>_quick_response_<voiceId>`; names come from the SDK `name` mapped from `voice_name`. Refresh uses `<device identifier>_refresh_quick_responses`. Setup starts an entry background LIST query without blocking startup. A failed query gets one LIST-only retry after 30 seconds; unload cancels the task. Success adds or restores buttons. Explicit refresh updates names, adds IDs and makes removed responses unavailable. Startup, reload, restored state, queries and refresh never call playback. Only an explicit button press calls `playQuickResponse(voiceId)` once, with a 75-second HA response timeout; the bridge awaits the SDK operation without a shorter deadline. A failed press is never retried automatically.

## SDK findings and acceptance limits

- SDK commit `6fea9a759379bd7ae8406c7e2e01adc2b7f024c9` makes `Device.describe()` resolve read labels with station context. The refreshed bridge forwards the SDK labels directly; its property-enum workaround is removed. Non-T9000 labels remain unchanged.
- The same SDK commit fixes cold indexing by promoting retained unknown parameters when station context resolves, preserving their observation timestamps. Binding resolves supplied station context as well. Reported 1421/1422 getters and composite writes now work on cold construction and every fresh `getDevice()` without another report. The bridge still suppresses raw configs from public state/events, and its projection tests now use cold and fresh models without a synthetic later report. A camera without a reported 2731 config refuses recording-quality writes cleanly.
- SDK rejection strings now omit station identity. The bridge accepts the sanitized strings when mapping finite error categories.

The bridge's SDK artifact and receipt now pin the cold-index fix. Both releases still need the lead's source review, image build, and separately authorized device acceptance. Concurrent app writes remain outside the SDK's read/merge/write isolation. No devices, hosts, containers or live HA were accessed for this implementation.

## Final SDK refresh versus the previous .16 candidate

SDK `c14bf36a6c20808eeeb9f4a905be762c943788ae` withholds setters outside its verified-parameter allowlist and serializes composite writes. The regenerated synthetic fixture shows `antiTheftDetection` changing from switch to binary sensor on all seven models; `workingMode` changes from select to sensor on T8170, T8214 and T8171. `snoozeTime` loses its setter on all seven models but already routed to a sensor, so its entity domain stays unchanged. Color temperature, RTSP publish and alarm volume have no exposed writable entity in the previous synthetic fixture; their withheld setters do not represent additional fixture domain changes. Actual retained reads without setters remain read-only.

Regeneration reused the original bridge `.offline/experimental16/compare.mjs` generator with the installed SDK, retaining the original .15 fixture baseline, then applied `normalize_properties` and `classify` to rebuild the .16 schemas and differences. Only the original schema fields are retained; identity/location values are excluded. No integration runtime or test code required changes.

The local SDK re-review refresh adds field-baseline ACK patches (120-second TTL), bounded/coalesced unsent writes, bounded quick-response timeout quarantine, and signaling identity redaction. The seven-model fixture was regenerated against the installed repack; all schemas and entity-domain changes are identical, so only its SDK commit pin changed. The original .15 baseline is retained. Integration runtime and package-allowlist files remain unchanged.

## Composite overlay history refresh

The SDK pin is now `c14bf36a6c20808eeeb9f4a905be762c943788ae`. The installed-package seven-model manifest regeneration matches all existing normalized schema fields and entity-domain differences; only fixture metadata changed. ACK history now survives confirmations and reports of intermediate writes for 120 seconds. An app edit to a retained ACKed value is indistinguishable from lag and the latest SDK ACK wins until expiry. Third values retire only their own patches. Integration runtime files are unchanged.


## Reviewed entity audit changes

The approved control audit adds **82 hidden-by-default controls**: 37 unknown sound controls
(15 type selects, 11 switches, 11 sensitivity numbers), 15 RTSP recording-mode selects,
15 diagnostic event-refresh buttons, station power-source and alarm-tone selects, eight
camera power-source selects and five motion-light-mode numbers. Arming mode stays visible.
Reported sound switches and sensitivity numbers retain their presentation, including controls
awaiting separate live testing. No sensor visibility or orphan cleanup is part of this change.
These rules have no overlap with active automation/dashboard references except arming mode,
which is explicitly excluded. No owner device identities are stored in source or fixtures.

New registrations use `entity_registry_visible_default`. Existing registry controls receive
`hidden_by=integration` only when currently unhidden. The per-entity registry option
`eufy_sdk.entity_audit_visibility_v1` records the decision once, including decisions to retain
visible reported sound controls. Reloads never override a later user unhide. User hides,
disables, names and unrelated registry options are preserved. The migration matches domain
and stable property unique ID, so renamed entity IDs remain safe. Users can unhide individual
controls in entity settings. For a controlled bulk rollback,
`audit_visibility.async_release_audit_hides(hass, entry_id)` releases only hides owned by this
migration and retains a released marker to prevent reapplication. Rollback source must retain
this helper until those changes are released; no registry rows are deleted.

**22 controls stop being created** on the audited T9000 setup: 13 non-doorbell loitering
switches (doorbells retain them), two accessory ringtone selects, two doorbell recording-quality
selects, four unwritable light switches and one station reboot button. SDK applicability and
write gates own the property changes; the bridge withholds reboot while RTC lacks a route.
Meaningful light reads, doorbell video quality and all surviving control identities remain.
Existing registry entries are retained as orphan/recovery material, subject to existing reversible
presentation handling. Other station topologies retain their previous behavior.

**Five motion-light duration numbers** use the upstream T8425/T8426 metadata bounds 30–900
seconds, retaining the existing integer step; upstream named timer values are 30, 60, 180, 300,
900. All five audit states were 180 seconds. Intermediate-second firmware acceptance remains
unproven. Numeric presentation expands rather than excluding a currently reported value.
**Three new percentage numbers** expose two T8214 ringtone volumes (1708) and station prompt
volume (1292), all 0–100. Existing ringtone sensors remain loaded with their original identities
and current values. Prompt volume is write-only/assumed state and uses the tested 1350 `{value}`
command with explicit mValue3 zero; it must not be described as confirmed reflected state.
The SDK exposes only installed write-only setters. The bridge joins these writes to the matching
reported property without changing the SDK's same-capability read/setter contract.

### Deferred app verification

No universal option set is established for the 15 night-vision selects. Leave their current
options/labels unchanged until the following model-specific checks are recorded. This includes
the existing potentially misleading Spotlight label on models without spotlights.

| Models | Eufy app screen | Record exactly |
| --- | --- | --- |
| T8425, T8426, T8170, T8171, T8416, T8134, T8214 | Camera settings → Video settings → Night Vision | Every displayed option and its raw 1277 value; determine whether 0/1/2 exist, whether 2 means color/spotlight and whether it is absent. In particular do not assume 2 on T8416/T8171. |
| T8425, T8426, T8170, T8171, T8416 | Camera settings → Pan and Tilt settings → Rotation Speed | Displayed labels and raw 6015 values for each stop; verify Slow/Medium/Fast is 1/3/5 and whether 2/4 exist. The SDK's T8410 capture does not establish these models. |
| T8425, T8416 | Camera settings → Sound Detection → Sensitivity | Displayed labels and raw 6044 values; verify Low/Medium/High is 1/3/5, and whether 2/4 exist. Four populated numbers stay numeric. |
| T8425, T8426, T8416 | Camera settings → Power Manager/Recording settings → Customize Recording | Minimum, maximum, increment or full choice list for Clip Length (1249) and Retrigger/Record Interval (1250). Preserve currently reported intervals 0, 120 and 300 seconds; unrelated battery defaults must not narrow them. Fourteen numbers keep existing bounds. |
| T8425, T8426 | Camera settings → Light settings → Motion-activated light → Duration | Confirm choices 30/60/180/300/900 seconds and whether arbitrary integer seconds are accepted; metadata bounds alone do not prove that. |

The exact translated screen names may vary by app version; the named feature and raw parameter
identify the check. The 44 deferred audit corrections comprise 15 night vision, 11 rotation speed,
four populated sound sensitivity and 14 clip/interval controls. Controls awaiting separate live
tests retain their current behavior, except the five explicitly approved hidden motion-light modes.
No version was bumped. No Home Assistant instance, device or phone was contacted.
