# Core compatibility boundary

The private installation's Core basis was **2026.10.1 with six local overlays**. Native framework qualification is narrower evidence than stock-Core media acceptance. This integration candidate includes none of those six Core files and never installs or overwrites Core.

| Item | Candidate status |
| --- | --- |
| Integration files and MIT license | Included |
| Paired SDK/bridge/add-on | Separate package |
| Six local Core overlays | Excluded |
| Optional scoped WebRTC player | Source-only; excluded from ZIP and ordinary HACS install |
| Stock-Core streaming / media playback | Unqualified |
| Camera switching | Unresolved / unqualified |
| ARM and non-S1 installations | Unqualified |
| General supported Core version range | Not established |

Inherited HACS floors `homeassistant: 2026.6.4` and `hacs: 2.0.5` are retained as metadata constraints. They are **not a tested support range** for this fork, do not describe the six private overlays, and do not qualify stock-Core compatibility. A version allowed by HACS is not an accepted environment. No new supported minimum/version range is asserted.

A working private environment must preserve and independently back up its exact Core image/version and modifications. This integration ZIP cannot restore them. Do not deploy overlays through this package or assume HACS reproduces the private streaming environment. A Core update and integration replacement require separate recovery decisions.

References, reviewed 2026-10-14: [HACS manifest keys](https://www.hacs.xyz/docs/publish/start/#hacsjson), [Home Assistant custom integration versions](https://developers.home-assistant.io/docs/creating_integration_manifest/#version).
