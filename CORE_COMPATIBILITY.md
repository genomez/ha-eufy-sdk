# Experimental.8 local preparation

Version `0.3.1-s1.experimental.8` is prepared locally and unpublished. Pair add-on `0.4.1-s1.experimental.8`, integration/bridge `0.3.1-s1.experimental.8` and SDK `0.2.1-s1.experimental.8`.

RTC late-viewer keyframe gating; bounded refresh backoff and recovery; isolated slow replies; camera connectivity and hub reply evidence; T8171 detection; opt-in floodlight safety with bounded confirmation and problem notices; observation-only media stall evidence.

Live view remains off by default; only boolean `webrtc_relay: true` enables it. Command keepalive remains 29000 ms. Integration floodlight safety uses `light_correction_enabled` and remains off by default. The opt-in safety binds HA intent, uses 15-minute motion and ON waits with a 30-minute watchdog, retries OFF until camera confirmation within its limits, and reports problems with bounded phone retries. It may retry an authorized ON intent; the historical .7 OFF-only corrector description below is superseded. Passive connectivity diagnostics do not establish physical state.

Media gap evidence is observation only; no automatic stream restart is added. T8113-Z firmware 3.3.8 picture-then-black remains unresolved. A second live-view open may still be needed. Brightness/spotlight formatting and unexplained floodlight ON events remain under investigation. The refresh retry/backoff addresses the prior .7 refresh issue.

This is an offline local candidate, with no release, registry, installation, account, device, media or new soak acceptance. Update and installation instructions in the historical text apply only after a separately approved publication.

## Historical published experimental.7 documentation

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
