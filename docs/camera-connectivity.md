## HomeBase camera connectivity

HomeBase S1 (T9000) per-camera status covers T8425, T8426, T8416, T8214,
T8134, T8170 and T8171, with mains or battery power. Owner-reported offline/recovery
testing covers T8425, T8426, T8416, T8214, T8134 and T8170.
T8171 is **unconfirmed: not offline-tested; assumed to behave like the other HomeBase cameras**.
These are prior owner observations; offline regression tests do not establish live camera behavior.

Qualification requires an unambiguous T9000 parent. Changing the parent model
retires the held assessment and old observers. The latest HomeBase p1131=1
(or true) means online; 0 (or false) means offline. Both hold without expiry.
p1131=2 or 3 means unknown. Other values remain raw diagnostics. Reports are
ordered by local arrival order; unorderable conflicts stay unknown, including
duplicates, until strictly newer evidence. Child media, push, cloud parameters
and command outcomes do not establish or replace this assessment. Other models
retain their existing contact-based rules.

Parent transport loss or unknown makes cameras unknown. After a HomeBase
reconnect, cameras show unknown until the next routine HomeBase status sweep
supplies a fresh report through existing mechanisms. The default cloud polling
interval is 600000 ms (10 minutes), scheduled after each poll completes; a
successful poll triggers the existing station-state sweep. Allow roughly
10 minutes plus poll/sweep time under healthy defaults. Failed or disabled
polling, busy transport or missing child reports can leave unknown indefinitely.
Existing optional station-state intervals retain their behavior. Reconnect adds
no reads, timers, retries or camera wakes and never replays a write.

Camera offline status never holds back light OFF: explicit OFF, retries,
persisted startup/restored OFF, rejoin OFF and watchdog/correction OFF bypass
the camera connectivity gate. Existing transport, cooldown, lifecycle, intent,
ownership and deadline checks still apply, so physical delivery is not guaranteed.
Automatic ON work retains its existing connectivity gate. The camera-health
contract remains `enforcement: "observe_only"` and `commandsBlocked: false`.
There is no new light option or command admission policy.

Upgrade SDK, bridge, integration and add-on together as a paired release.
Source changes alone do not update the add-on runtime mirrors; packaging,
provenance and physical acceptance remain separate release steps.
