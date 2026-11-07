# Local playback modifications

## Recording day folders, 2026-11-07

- Preserve camera folder names and group playable, available recordings into Chicago-local calendar days, newest first: Today, Yesterday, then labels such as Thu, Nov 5. Always show the day level, even for a single day. Time unknown remains the last category; empty or hidden-only days are omitted.
- Show clip grid titles as time and duration, such as 10:31 AM · 17 s, with duration unknown when absent. Preserve sub-minute seconds, minute formatting, covers, camera status disclosures and refresh behavior. Focused clip detail retains its date and diagnostic context.
- Build folders from one existing catalog response per browse. Day navigation uses /d/YYYY-MM-DD; clip identifiers remain the original camera-level /e/ paths, so existing playback links still resolve. No additional station calls, scheduling or limits are introduced.
- Synthetic offline coverage includes Chicago midnight, both DST transitions, relative day labels, unavailable and unplayable rows, empty days, thumbnails and playback from a day folder. Local changes only; no device or installation acceptance is claimed.

## Playback 3p7, 2026-11-06

- Bound save-first playback waits by 150 seconds admission + three times duration + max(120 seconds, six times duration) conversion + 120 seconds grace. Grace includes capture/cleanup reserve, bounded reconnect and conversion termination. Clamp the total to 2,147,483 seconds; progress extensions stay anchored to the original start.
- Preserve cancellation of the resolving waiter and existing shared-job isolation. Immediate preparation/status RPCs retain their three-second bound.
- Identify WebSocket connections with the optional X-Eufy-Client: ha-integration header. Older bridges ignore the header; no protocol or authentication change is required.
- Synthetic offline tests only; no deployment or device acceptance is claimed. Existing license and modification notices remain in INTEGRATION_NOTICES.md.

## Playback 3p7r warning coverage, 2026-11-06

- Log common clip-unavailable and timeout playback failures once at WARNING with a bounded reason and the existing value-free failure site. Unknown reason text is reduced to failed; identifiers and exception values are excluded.
- Add synthetic warning/privacy regressions. The playback deadline formula and queue budgeting are unchanged; no deployment or device acceptance is claimed.
