# S1 experimental.16

Pair: add-on **0.4.1-s1.experimental.16**, integration/bridge **0.3.1-s1.experimental.16**, SDK **0.2.1-s1.experimental.16**.

## HomeBase S1 settings
- Settings sent through T9000 use the camera's assigned channel. Unsupported controls are hidden, and changes to one part of a shared setting preserve the other parts. Recent successful changes are protected from lagging reports for a bounded period.
- Installed speaker and doorbell volume controls are available where supported. HomeBase prompt volume shows its reported value.
- Sound detection is hidden except on S350 (T8416), where the app offers it.

## Recording and lighting
- Recording quality follows the model's supported tiers, including 4K on S350 (T8416) and Auto on T8170. Existing recording-quality selections may show updated labels; check automations that select them by name.
- Motion-light activation offers Smart and Fast where supported, with the supported timer choices. Structured light schedules can be set through the integration service.
- Night-vision choices follow the supported modes and app order. Clip-length minimums follow the model's app settings; mains-powered cameras can show reported clip settings without offering unsupported writes.
- Doorbell quick responses have individual buttons and an explicit refresh action. Refreshing the list does not play a response.

## State and diagnostics
- A new **Solar charging** binary sensor shows reported solar power where supported. It is new since .15; existing users do not need to rename a sensor. Unsupported devices and the HomeBase itself do not gain a synthetic charging state.
- Write-only switches restore their last successful setting across reloads and outages. Failed or superseded writes do not replace that saved value.
- Retained device handles keep their channel, settings and reported state as device lists and station context refresh.
- An on-demand diagnostic query can request a device's reported solar details. It runs only when requested and reports the full reply or a rejection reason.

## Upgrade
Update the add-on and integration together, then restart Home Assistant after the add-on is running. Installation and restarts require separate approval; this local candidate has not been deployed.

## Known limits
On SoloCam E30 (T8171) behind a T9000, the image-flip switch is write-only. In testing it changed the pan/tilt aim and did not reliably set picture orientation. Avoid using it to correct orientation.

Playback is silent; multi-lens cameras show one view. Shared-setting writes cannot prevent simultaneous edits in the app. Offline tests and the no-account smoke check do not establish live-device acceptance.
