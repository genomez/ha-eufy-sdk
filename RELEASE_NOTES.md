# S1 experimental.17

Update the add-on and integration together, then restart Home Assistant after the add-on is running.

| Component | Version |
| --- | --- |
| Add-on | 0.4.1-s1.experimental.17 |
| Bridge | 0.3.1-s1.experimental.17 |
| Home Assistant integration | 0.3.1-s1.experimental.17 |
| SDK | 0.2.1-s1.experimental.17 |

## Privacy and state

- The S350 (T8416) on HomeBase S1 gains a writable Privacy switch. A fresh station read-back confirms the requested value, including a write that matches the current state. The integration reports when confirmation is unavailable.
- Bridge snapshots prioritize privacy, guard-mode, and floodlight evidence when they reach the 128-receipt limit. Invalid entries leave room for valid evidence.
- Floodlight switches use fresh device reports to reconcile state. Home Assistant excludes transient floodlight receipt attributes from the recorder.
- The T8214 doorbell spotlight switch is available through HomeBase S1. HomeBase battery health and estimated battery life appear when the station reports them.

## RTC and recordings

- When a replacement RTC connection fails, the SDK can retain the healthy session and retry renewal. Diagnostics retain bounded failed-connection history and link retries.
- The early RTC offer cutoff defaults to off. The overall connection deadline still applies. Set EUFY_RTC_OFFER_WAIT_MS to a positive integer in milliseconds to opt in.
- Unchanged recording catalogs skip disk rewrites. Catalog pacing survives sidecar write failures.

## Known limits

On SoloCam E30 (T8171) behind a T9000, the write-only image-flip switch can change the pan/tilt aim without reliably setting picture orientation. Avoid using it to correct orientation.

Playback is silent. Multi-lens cameras show one view. Simultaneous edits in the Eufy app can race shared-setting writes.
