# Browse-only recordings

With a bridge advertising recordings contract v1 and a valid five-camera scope,
the integration enables **Browse recordings** by default. Disable it in the
integration options to unload recording interests and pending thumbnail requests.
A bridge without the capability keeps its existing camera, image and light behavior.
No research probe is used as a fallback.

Open **Media → Eufy → camera**. Root browsing reads only the bridge's cached camera
list. Entering a camera asks for a metadata refresh if due and returns the current
cache immediately. Use the media browser's Refresh action after the requested
refresh or a pending cover completes. Nothing polls recording catalogs or
prefetches covers in the background.

The HomeBase query sees the latest 20 station-wide events. The bridge accumulates a
seven-day, evictable catalog, capped at 2,000 rows total and 400 per camera. Each
camera browse returns up to 100 newest events; a truncated result is disclosed,
without pagination. An empty folder does not prove that the camera has no history.
“May no longer be on HomeBase” marks retained events outside the last successful
station window. Every event is an expandable information folder: video playback,
downloads and media resolution are unavailable in Phase 1.

Event titles start with Chicago local `YYYY-MM-DD HH:mm:ss · type · duration` for
Advanced Camera Card date parsing. Unknown times have an explicit status. Event
details include UTC when known. Unknown numeric event types use `Event <code>`;
unqualified vendor labels are never displayed.

Thumbnail demand uses a 60-second camera-folder interest and the bridge's shared
admission budget (up to four uncached covers). Only a ready WS reply allows a
cache-only HTTP GET to the fixed configured bridge origin. Thumbnail URLs exposed
to the frontend contain opaque IDs and an optional opaque cache revision, never a
bridge URL, serial, path, credential or interest token. HA's normal HTTP middleware
authenticates every thumbnail request, including RAM-cache hits. All authenticated
HA users can access this feature; there is no additional per-user recordings ACL.

HA keeps at most 32 covers / 16 MiB in RAM for 60 seconds, and waits up to ten
seconds for a pending cover before returning a neutral PNG. It keeps no disk
archive or Recorder history. The bridge separately limits covers to 256 files /
128 MiB with a seven-day TTL. Reload, disconnect, scope replacement and disabling
clear HA's interests and pending work without deleting the bridge cache.

Integration diagnostics exposes `folder_media_id` and `navigation_path` for each
camera (reading the cached camera list if needed). Camera renames do not change these IDs:

```javascript
const folder = `media-source://eufy_sdk/s/${scopeId}/c/${cameraId}`;
const navigation_path = '/media-browser/browser/' + encodeURIComponent(',' + folder);
```

Use that relative `navigation_path` in a Lovelace `navigate` action. The encoded
leading comma is the empty content type; the remainder is the complete media URI.
This also targets the signed-in Android companion WebView. No dashboard is changed
by this integration. Before deployment, qualify the frontend route and authenticated
relative thumbnails on the actual desktop and Android versions, and check that an
anonymous thumbnail request is refused. Local synthetic unit tests do not establish
those live UI behaviors or qualify the bridge/SDK/device pairing.
