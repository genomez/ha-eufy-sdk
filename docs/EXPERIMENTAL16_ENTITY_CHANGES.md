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

The fixture `tests/fixtures/t9000_settings_manifests.json` records the .15/.16 schemas and exact generic-platform differences for all seven models. It uses SDK .15 `18b34ff` and .16 `85c3cc2`, with the .15 bridge's scalar hiding applied. Test device types: 47, 104, 48, 93, 19, 48, 87 respectively. Supplied parameters are synthetic capability evidence, not proof that every model reports every test parameter. Quick-response additions are dynamic and recorded separately from generic properties.

## Retained writable controls and values

When described writable by the SDK, the existing switch/number/select unique IDs remain unchanged: `enabled`, `statusLed`, `motionDetection`, `imageFlipped`, `watermark`, `microphone`, `speaker`, `speakerVolume`, `audioRecording`, `recordAutoStop`, `recordDuration`, `recordInterval`, `brightness`, and `spotlightEnabled`. Doorbells retain `wdrSwitch`, `chimeSwitch`, `mechanicalChimeSwitch`, `videoQuality`, `ringtoneVolume`, `dingdongVolume`, and `dingdongRingtone` where installed. Missing boolean setters give binary sensors; missing numeric/enum setters give sensors. Readless settings are omitted. Legacy privacy entries are retired on T9000 because the SDK removes its write-only setter and supplies no privacy reading.

Recording quality uses the existing `recordingQuality` identity with `720P` = 1, `1080P` = 2, `3K` = 3. Previously shown labels were `HD (720P)`, `Full HD (1080P)`, `Max`. Automations selecting labels must use the new labels. Active-mode reads may remain unchanged after a write to mode 0. 720P has no hardware acceptance. The bridge's old brightness/spotlight/color-temperature block is removed; color temperature is only offered if the SDK actually supplies a property. No new color-temperature control is invented for white-only floodlights.

## Schedule and quick-response actions

`lightSchedule` is a sensor: state is entry count, `entries` is the sanitized list. Location and identity fields are omitted recursively; the full surrounding config is never an attribute. No text editor entity is created. Call `eufy_sdk.set_light_schedule` with an HA `device_id` and `entries` list; `[]` clears it. The SDK validates the reported backing config and preserves its other fields. No entry schema or mode labels are inferred. Motion-light duration and brightness use SDK bounds; mode is an integer number with no invented labels.

Quick-response unique IDs are `<device identifier>_quick_response_<voiceId>`; names come from the SDK `name` mapped from `voice_name`. Refresh uses `<device identifier>_refresh_quick_responses`. Setup queries once; explicit refresh updates names, adds IDs and makes removed responses unavailable. Startup, reload, restored state, queries and refresh never call playback. Only an explicit button press calls `playQuickResponse(voiceId)` once. A failed press is never retried automatically.

## SDK findings and acceptance limits

- `Device.describe()` omits station context when resolving read labels. The bridge uses the SDK's correctly resolved `recordingQuality` property enum map, without changing SDK code or non-T9000 labels.
- SDK facade construction applies parameters before T9000 station context. Configs 1421/1422 initially remain under `unknown_1421` / `unknown_1422`; their new getters return empty and composite writes can refuse until another report reindexes them. A fresh `getDevice()` can recreate this condition. This is an unresolved SDK blocker for reliable schedule/motion-light operation, not a bridge-side retry opportunity. The bridge suppresses both named and unknown raw configs from public state/events. Tests cover the cold privacy boundary and a later-report model's decoded presentation. SDK source is untouched.
- SDK rejection strings now omit station identity. The bridge accepts the sanitized strings when mapping finite error categories.

Both releases still need the lead's source review, corrected SDK artifact for the cold-index issue, image build, and separately authorized device acceptance. Concurrent app writes remain outside the SDK's read/merge/write isolation. No devices, hosts, containers or live HA were accessed for this implementation.
