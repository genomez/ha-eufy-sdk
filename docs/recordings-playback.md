# Saved recording playback

Playback is opt-in through the bridge's additive recordings capability:
`playback: true`. Missing, false or non-boolean values disable playback and leave
camera folders visible with a "no playable recordings" status. All existing
capability checks remain required, including `browseOnly: true`; this historical
field describes the original catalog contract. The bridge must advertise the playback flag only when its
`RECORDINGS_PLAYBACK` switch is enabled.

Camera listings show only available rows with exact-event playback authority
(`playable: true`) while the playback capability is enabled. This includes
retained playable events outside the latest station window. Focused event links
keep their information view and existing playback errors for unavailable video.
"May no longer be on HomeBase" remains a detail-only uncertainty notice.

The v1 bridge catalog supports neither a playable-only request nor a cursor.
HA filters the returned page without extra requests or changes to its stored
catalog and truncation flag. "Catalog limited to newest 100" describes the
underlying catalog bound, not a count of playable results: hidden rows can leave
a short or empty page, and older playable rows beyond that bound cannot be
listed through this protocol. An empty view does not prove there is no history.

Tapping an event requests a focused catalog to validate camera enrollment and
event membership, then calls `recordings.clip_prepare` once. Resolve remains
pending while queued, pulling or converting. The HA frontend controls its normal
loading display; this integration does not add a progress dialog or percentage.
Change notices wake existing waiters, with a two-second `clip_status` fallback
and a half-second minimum between notice-driven status requests.

The deadline starts with the event duration plus 60 seconds (180 seconds when
duration is unknown). A longer bridge duration/expected duration extends it from
the original start, never from the latest poll. Validated durations are bounded
to 24 hours, matching the existing catalog bound. A timed-out viewer gets a short
retry message. Cancellation releases that viewer's waiter and does not call
`clip_cancel`, because the idempotent job may serve other viewers. The bridge
must enforce finite job expiry and give live view priority independently of HA.

Ready clips resolve to the relative HA path
`/api/eufy_sdk/recordings/{scope}/{camera}/{event}/clip.mp4`, signed by HA's
`async_sign_path`. The signature lasts at least 15 minutes, or the clip duration
plus five minutes. Bridge expiry can make the clip unavailable sooner. HA's
standard authentication middleware validates the path signature and rejects
unsigned anonymous requests; the view retains `requires_auth = True`.

The authenticated proxy requests only the fixed bridge path
`/recordings/v1/clips/{camera}/{event}.mp4` using the same session and origin policy
as covers. It forwards a single byte Range and validates 200/206 framing, including
Content-Length and Content-Range, and passes through 416 with its unsatisfied
Content-Range. Redirects, compressed payloads and non-MP4 content are refused.
Bytes stream in 64 KiB chunks with backpressure and no full-file cache. Opaque
scope, camera and event checks remain in force, including during streaming.

## Bridge reconciliation

- Clip RPCs use the existing envelope: `{ok: true, recordings: {clip: ...}}`.
  Only the nested clip fields from the agreed contract are retained. Durations,
  expected duration, bytes and expiry may be null; pulled duration is an integer.
- Catalog rows advertise exact-event authority through the `playable` boolean.
  HA requires both that authority and the additive playback capability.
- Clip change notices use the existing version/scope/camera/revision envelope
  with reason `clip`. Other accepted reasons still wake waiting resolves.
- Supported reasons are `none`, `live_view`, `busy`, `owner_unavailable`,
  `not_found`, `too_long`, `storage`, `convert_failed`, `station_error`,
  `cleanup_uncertain`. Other values fail safely without displaying wire text.
- The bridge serves ready files with video/mp4, Content-Length and standard
  single-range 206/416 framing. It owns storage limits, job expiry, admission,
  cleanup and live-view preemption.

Offline tests exercise these assumptions with synthetic data, HA signing/auth
middleware, and in-memory HTTP requests. Actual HA frontend resolve timeouts,
Android/Desktop loading behavior, bridge expiry timing, seeking, and H.264/MP4
codec compatibility require later authorized acceptance tests. Audio is optional.
