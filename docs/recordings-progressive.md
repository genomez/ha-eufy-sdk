# Progressive event playback candidate

An additive bridge `progressivePlayback` capability permits early resolution of
qualified, buffered HLS EVENT recordings. Older bridges and unknown/long catalog
durations keep the existing completed MP4 path. A ready cached clip always
resolves as MP4 without starting a new capture or encoder.

The integration proxies a validated bounded segment inventory through HA views
with `requires_auth = True`. The playlist and each child segment use separate
path-bound HA signatures. Previously published child references are immutable.
The browser receives no bridge origin, source selector, filename or credentials.
Inventory or session failures return an explicit terminal error; incomplete
capture never receives ENDLIST. Segment bytes have strict MIME/length framing,
64 KiB backpressure-aware proxying and per-chunk entry validation. Redirects are
disabled. GET/HEAD can renew only an existing finite lease, never admit a pull.

Sessions bind the integration's scope and runtime generation, camera/event,
bridge job generation and hard expiry. Bridge-side expiry also handles HA loss.
Reset/unload releases local sessions best-effort. Pending resolve cancellation
releases that viewer alone. Authenticated media reads renew a 30-second idle
fallback lease. An expired local session fails closed and must be resolved again.
Pausing or buffering without further HTTP requests may expire a presentation.

This is backend transport support for the stock media dialog. It cannot observe
every dialog close or guarantee the frontend starts at zero, disables live-edge
catch-up, exposes growing seek controls or displays a descriptive terminal reason.
An owned player/control surface is not included. Browse items retain MP4 metadata;
HLS resolves return `application/vnd.apple.mpegurl`. Actual frontend MIME routing
must be accepted on Android companion and desktop before enablement.

The bridge candidate requires explicit `RECORDINGS_PROGRESSIVE_FPS=15` and remains
default-off until that exact constant media cadence is qualified. Real ffmpeg
decode, independent TS seeking, MP4 finalization and both-client live acceptance
remain required. Include 2x delivery, 1x retry, arrival pauses, rapid reopen,
two viewers, idle/background expiry, live preemption, failure, signed child auth
and instant cached-MP4 seek/replay. No deployment or device access occurred.

The inherited manual-package manifest/tooling describes an older pinned archive
and was already stale at the starting revision. This source candidate does not
refresh that archive. Reconcile its runtime allowlist and hashes before building
a manual installation artifact; normal repository installation includes the new
runtime module.
