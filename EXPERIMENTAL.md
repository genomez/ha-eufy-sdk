# S1 experimental.14

Pair: add-on **0.4.1-s1.experimental.14**, integration/bridge **0.3.1-s1.experimental.14**, SDK **0.2.1-s1.experimental.14**.

## Fixed
- S350 (T8416) cameras no longer show a spotlight switch they don't have.
- A push connection that silently stops delivering events is now detected and reconnected.
- Camera settings are no longer overwritten by older values when a startup or refresh overlaps a change.
- Snapshot refreshes are no longer lost when they overlap; queued refreshes stop after a sign-in failure.

## Changed
- Floodlight brightness/spotlight controls that HomeBase S1 can't drive are hidden.
- Number settings use per-model ranges.
- Media → Eufy shows "Playback not yet supported for this camera", "No playable recordings yet" or "No recordings yet" instead of an empty camera folder.
- More playback diagnostics.

## Upgrade
Update the add-on and integration together, then restart Home Assistant after the add-on is running, so removed controls are cleaned up.

## Known limits
Playback is silent; multi-lens cameras show one view; a late update from a camera can still briefly show an old state.

## Use and rollback

This is unofficial, experimental HomeBase S1 support, tested mainly in one household. Linux amd64 is supported. Run only one bridge on the account. Back up the integration and add-on and retain the previous paired versions for rollback; restore both together and restart Home Assistant.

Recorded playback starts while the clip is still being fetched by default. Recording times and day folders use Home Assistant's time zone. Live view and optional floodlight safety remain opt-in. Keep battery-camera preload off. Command keepalive remains 29000 ms.

Restart the add-on after changing any option. **Extra playback camera models** lets you try models outside the tested seven; it does not guarantee playback. **Cameras to skip for recordings** removes those cameras from recording lists, thumbnails and playback.

Recordings require exactly one HomeBase S1 (T9000). Recent-history coverage is limited. Live view takes priority, so playback can wait or fail while the HomeBase is busy. The cause of an earlier playback stop remains unknown.

Review diagnostics before sharing them. Never post credentials, session files, serial numbers, private network details or private footage. Preserve the bundled licenses and source-access notices when redistributing.
