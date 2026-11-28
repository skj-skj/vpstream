# Video call while watching together: plan

Status: design agreed 2026-11-28 (decisions at the end); not built yet. Mockup:
`/storage/emulated/0/Download/vpstream-notes/call-mockup.png`.

## What the user asked for (2026-11-28)

- A video and audio call between the viewers of a watch-together stream, in the new (touch)
  player only. Browser to browser over WebRTC, so the video and audio never pass through
  the VPS.
- **Where the tiles go:** first the black bar on the right of the video, then the one on the
  left. Each bar holds 2 tiles, so 4 show at once. Other viewers come first and your own
  tile comes last. With 5 or more people your own tile drops off page 1, but your camera
  is still sent. With no side bars on a device, the tiles lie over one side of the video.
- **Pages:** 4 tiles per page. With 8 people, page 1 shows 4 and page 2 shows the next 4,
  your own tile included. Previous and next buttons appear when more than 4 are in the call.
- **Starting a call:** someone presses Call and every other viewer gets a prompt to accept.
  After that, anyone joins or leaves whenever they like, including someone who left earlier.
- **Ending:** with 2 in the call, either one pressing End ends it for both. With 3 or more,
  End takes only that person out.
- **Your own controls:** camera on/off ("hide"), mic on/off ("mute"). Nobody can mute or
  hide anyone else. A muted person shows a mic-off icon at the bottom right of their tile.
- **Zen mode:** hides every tile for you alone. You still hear the others, and they still
  see you and each other.
- **Player controls, in order:** Call, then in a call camera, mic, Zen, and page previous
  and next.
- **Front or back camera.**
- **Limits:** usually 2 people, at most 3 or 4, with a hard cap of 20.
- **Priority:** the film plays first. The call gets what is left and never costs the VPS
  bandwidth.
- **Vendored library:** any third-party library is kept in the repository, like hls.js, with
  a weekly update workflow.

## Design

### 1. Transport: plain WebRTC, with the daemon passing signaling messages (recommended over PeerJS)

PeerJS is a wrapper around the browser's own `RTCPeerConnection`. It always needs a
**PeerServer**, a WebSocket server that passes the offers, answers and ICE candidates
between browsers. That gives three choices:

| Option | Server | Third party | VPS cost |
| --- | --- | --- | --- |
| a. PeerJS + PeerJS's free cloud server (0.peerjs.com) | theirs | yes: it learns who connects to whom, and the call breaks when it is down | none |
| b. PeerJS + our own PeerServer | Node.js on the VPS, Termux and Windows | no | a new runtime to install, update and secure on 3 platforms |
| c. **Plain WebRTC + the daemon as the go-between** | the daemon we already run | no | a few small JSON messages per join |

Option c needs **no library at all**, so there is nothing to vendor and no supply chain to
watch. The daemon (threaded werkzeug behind Caddy) has no WebSockets, but it does not need
them: signaling is a handful of messages of 1 to 6 KB each, sent when someone joins. They
travel on the routes the page already uses:

- **Sending:** `POST /ALIAS/N/call` with `{to, kind: offer|answer|ice|bye, data}`. The
  request must come from an approved device's page in the room, and its size is capped.
- **Receiving:** the existing `sync` long-poll, which wakes when the page has mail. Its
  answer carries `call: {id, members, mail: [...]}`. That costs no new held connection and
  no new Caddy route.

The daemon also keeps the **call state**, which the end-for-both rule needs. It lives in
`Room` (or in a small `Call` beside it in together.py, with no I/O, tested like Room):

- `call_id` (a new one for each call), `members: {page: {name, joined, mic, cam}}`,
  `rang: set(page)` and `mailbox: {page: deque}`, with caps (20 members, mailbox size,
  message age).
- `start(page)`: if no call is running, starts one with this page and rings every other
  page. If a call is running, the page joins it.
- `leave(page)`: if 2 were in the call, it ends for both (the other page gets `bye`). With 3
  or more, only this page leaves. A page that goes quiet (PAGE_SECONDS) leaves.
- Mic and camera state is sent through the daemon, not only over WebRTC, so a tile shows
  "muted" even while its peer connection is still coming up.

Should the user still want PeerJS (option a or b), section 7 describes the vendoring.

### 2. Topology: a full mesh, capped at 20

Every pair of browsers in the call has its own `RTCPeerConnection`. To avoid both sides
making an offer at once, the page with the lower id makes it. With 2 to 4 people this is the
standard approach. At 20 it is heavy (19 uploads per browser), so:

- **Video only goes where it is shown.** A tile that is not on screen (another page, Zen,
  or the tab hidden) tells its sender to stop. The sender then sets that peer's video
  sender inactive with `setParameters({encodings:[{active:false}]})`, and stays
  renegotiation-free. A viewer therefore receives at most 4 videos, plus audio from
  everyone.
- **Audio always flows** (Opus, about 24 to 32 kbps per peer). With 20 people that is
  roughly 0.6 Mbps of upload per browser, which is acceptable.
- **Video size per tile:** 320×240 (or 240×320) at 15 fps, `maxBitrate` 150 to 250 kbps,
  `degradationPreference: "maintain-framerate"`, and `contentHint: "motion"` left off.

### 3. The film comes first

- Call video is capped as above, and the total video upload is spread over the peers that
  watch it.
- **While the film waits for data** (the `waiting` event, or the hls.js buffer under about
  5 s), every outgoing video is paused and only audio flows. Video resumes once the buffer
  is healthy again for a few seconds.
- The call never starts by itself. A page that is not in a call opens no connections and no
  camera.

### 4. Layout: tiles in the black bars

The player knows the stage size and `video.videoWidth/Height`. With `object-fit: contain`:

```
scale = min(stageW / vw, stageH / vh); drawnW = vw * scale
sideBar = (stageW - drawnW) / 2
```

- **sideBar ≥ 96 px** (to be tuned on the phone): the right bar holds tiles 1 and 2,
  stacked between the top bar and the bottom controls, filling the bar's width at about
  3:4. The left bar holds tiles 3 and 4.
- **sideBar < 96 px** (a 2.39:1 film on 16:9, or a phone held upright): a column of small
  tiles over the video on the right, about 18% of the stage width.
- Laid out again on `resize`, `loadedmetadata`, rotate and full screen. Tiles sit inside
  `#stage`, so they come along into page full screen and rotate with it. On an iPhone, in
  its own full-screen player, no tiles show (iOS allows nothing over its player), but the
  audio keeps playing.
- **Order:** the others in the order they joined, then you. That list is cut into pages of
  4. With 4 people: right 1 and 2, left 3 and you. With 5 people, page 1 is the 4 others and
  page 2 is you. "Speaking" (a yellow ring, from the `audioLevel` in `getStats`) is a nice
  extra and does not change the order.
- **A tile:** `<video autoplay playsinline muted>` (the sound plays through one hidden
  `<audio>` per peer) with `object-fit: cover`, the person's name, and a mic-off icon at the
  bottom right. With the camera off, the tile shows their initial (as on the toast).

### 5. Controls (new player only, while watching together)

The bottom row, left of Subtitles (see the mockup):

- **Not in a call:** one green **Call** button. Pressing it asks the browser for the camera
  and mic, then starts the call or joins the running one.
- **In a call:** **End** (red), **Mic**, **Camera** (a long press or a second button
  switches front and back), **Zen**, and **‹ n/m ›** when more than 4 are in the call.
- **Ringing:** the other pages show a prompt in the middle: "Asha started a call. Join /
  Not now". The film keeps playing. Not now closes it; the Call button stays, with a dot
  while a call is running.
- The double-tap and center-tap zones ignore taps on tiles.
- The controls' 10 s cooldown after joining the group does not apply to call buttons.

### 6. Security and privacy

- Only approved devices of a watch-together stream can signal, and only to pages in the
  same room. Messages are size- and rate-capped, and their content is opaque to the daemon
  (SDP and ICE are passed through, never interpreted).
- **WebRTC shows each browser's IP address to the others in the call.** They are people the
  owner let in, but this goes in the README.
- Media is always encrypted (DTLS-SRTP). The daemon never sees it.
- The camera and mic are released (`track.stop()`) when the call ends for you, and when the
  page is hidden for longer than N minutes (to be decided).
- Caddy and Flask headers: a `Permissions-Policy: camera=(self), microphone=(self)` on the
  player page, and nothing for other origins.

### 7. If PeerJS is kept: vendoring

As with hls.js: `src/vpstream/assets/peerjs.min.js`, `peerjs.LICENSE` (MIT) and
`peerjs.version`. Add `scripts/update-peerjs.py`, which fetches `peerjs` from the npm
registry and checks the tarball's sha512 `integrity`, and
`.github/workflows/peerjs-update.yml`, a copy of hlsjs-update.yml. Serve it in
`player.ASSETS` and load it only when the stream is watched together. (With option c, none
of this is needed.)

### 8. NAT traversal

- **Same Wi-Fi** (Termux, Windows, local mode): host candidates are enough, with no outside
  server.
- **Over the internet** (VPS): browsers need a **STUN** server to learn their public
  address. Google, Cloudflare and others run free ones. Alternatively, the daemon could
  answer STUN itself on UDP 3478, which is about 60 lines of Python and almost no
  resources, but needs the port open in ufw and in the provider's firewall.
- **TURN** (relaying media when two NATs refuse to meet, which is common on mobile data
  behind CGNAT) **passes the media through a server**, which is exactly what the user wants
  to avoid. Without TURN, such a pair gets "Could not connect to Asha" on their tiles while
  the rest of the call works.

### 9. Where calls work: HTTPS only for the camera and mic

Browsers allow `getUserMedia` only in a **secure context**, which means HTTPS or
`localhost`. That covers the VPS with `init --https`. It does **not** cover Termux, Windows,
`init --local` or `start --http`, which serve plain HTTP on a LAN address. There, the call
can at most be **receive-only**: you see and hear the people on HTTPS, and send nothing. On
an all-LAN setup that means nobody can send. Making the LAN case work would mean HTTPS on
the LAN (a local CA the phones must trust), which is a project of its own.

### 10. Sound

Playing the film through speakers while the mic is open sends the film to the others,
slightly late, as an echo. Chrome's echo cancellation removes the call's own audio, but not
reliably the `<video>` element's audio on every platform. Mitigations: headphones (a hint
on first join), the mic muted by default on join, and `noiseSuppression` and
`echoCancellation` on. Lowering the film's volume while someone speaks is possible later.

## Files to touch

- `together.py`: call state and mailbox (`Call`), part of `state()` and `describe()`.
- `stream.py` and `player.py`: the `call` POST route, and mail added to the sync answer.
  `caddy.py`: route `/call` like `sync` if needed.
- `assets/call.js` (new, ES5 like the rest): peer connections, media, the bitrate rules and
  the layout of the tiles.
- `assets/modern.html` and `modern.js`: buttons, prompt, tiles and hooks.
- `assets/sync.js`: pass the call part of the sync answer to call.js.
- Dashboard Viewers tab: who is in the call (read-only).
- Tests: `test_together.py` (call rules: ring, 2-end-both, 3-leave-one, the 20 cap, mail
  caps), `modern_test.js` (layout math: bars, pages, order) and a `call_test.js` with a fake
  `RTCPeerConnection`.
- Manual tests: 2 phones on the VPS over HTTPS (Wi-Fi plus mobile data), the laptop, and
  the iPhone (camera and full screen not device-tested yet, see the memory notes).
- README: the Watch together section, IP visibility, and HTTPS needed.

## Decisions (2026-11-28)

1. **Transport:** plain WebRTC with the daemon passing the setup messages (section 1,
   option c). No PeerJS, no vendored library and no update workflow (section 7 is dropped).
2. **HTTP streams:** calls work on HTTPS only. The Call button is hidden where the page is
   not a secure context (Termux, Windows, `init --local`, `start --http`).
3. **NAT:** a public STUN server (Cloudflare `stun.cloudflare.com:3478`, Google as a
   second). No TURN. A pair that cannot connect shows "Could not connect" on that tile.
4. **Joining:** the camera starts on and the mic muted, with a one-time hint to use
   headphones.
5. **A phone held upright:** the tiles go in the bar below the video, then the one above,
   in a row of 2 each. They overlay the side only when no bar, top, bottom or side, is big
   enough. In section 4, the "bars" are the side bars when the video is narrower than the
   stage, and the top and bottom bars when it is wider.
6. **Ringing:** every new call rings every viewer who is not in it, including after a
   2-person call ended for both. A viewer who opens the stream while a call runs also gets
   the prompt. Joining a running call through the Call button needs no ring.
7. **Availability:** always available in every watch-together stream served over HTTPS,
   with no switch. Nothing starts until someone presses Call.
8. **Camera off:** the tile stays and shows the person's initial, with their name and the
   mute icon.
