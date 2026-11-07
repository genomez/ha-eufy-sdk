# Local playback modifications

## Playback 3p7, 2026-11-06

- Bound save-first playback waits by 150 seconds admission + three times duration + max(120 seconds, six times duration) conversion + 120 seconds grace. Grace includes capture/cleanup reserve, bounded reconnect and conversion termination. Clamp the total to 2,147,483 seconds; progress extensions stay anchored to the original start.
- Preserve cancellation of the resolving waiter and existing shared-job isolation. Immediate preparation/status RPCs retain their three-second bound.
- Identify WebSocket connections with the optional X-Eufy-Client: ha-integration header. Older bridges ignore the header; no protocol or authentication change is required.
- Synthetic offline tests only; no deployment or device acceptance is claimed. Existing license and modification notices remain in INTEGRATION_NOTICES.md.

## Playback 3p7r warning coverage, 2026-11-06

- Log common clip-unavailable and timeout playback failures once at WARNING with a bounded reason and the existing value-free failure site. Unknown reason text is reduced to failed; identifiers and exception values are excluded.
- Add synthetic warning/privacy regressions. The playback deadline formula and queue budgeting are unchanged; no deployment or device acceptance is claimed.
