# vpstream

On-demand video streaming from a VPS. The public port is open only while a stream runs, and
everything is controlled over SSH or a dashboard on your tailnet (or, if you choose, a public
dashboard over HTTPS behind a password).

Status: early development. Streams a file or a folder as it is, for players such as VLC and
mpv (direct mode), or as prepared HLS versions with a player page for TV browsers (browser
mode).

## Install

Needs Linux, Python 3.12+, Caddy, ufw, systemd, and Tailscale with HTTPS certificates (not
needed for a public dashboard).
ffmpeg is used by browser mode, `bench` and `caps`; mkvtoolnix and the `acl` package are
optional (see below). `vpstream doctor` checks all of it and prints the fix for anything missing.

```text
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"
vpstream doctor
sudo vpstream init
```

The install must be system-wide (the service runs as its own user). Upgrade with the same
prefix and `pipx upgrade vpstream`, then `sudo vpstream init` again.

## Commands

| Command | What it does |
| --- | --- |
| `vpstream start --file F` / `--dir D` | Starts a stream of one video, or of a folder's videos |
| `vpstream ls [FOLDER]` | Lists a folder's videos (default `[media] folder`, `/srv/media`), their tracks and what is prepared |
| `vpstream track-info FILE` | Lists a file's tracks with the numbers `--sub` and `--audio` take |
| `vpstream prepare --file F` / `--dir D` | Makes the versions browser mode plays (`--sub 1,3`, `eng`, `none`) |
| `vpstream prepared [rm PATH \| --stale \| --all \| cancel [PATH]]` | Lists, removes or cancels prepared versions |
| `vpstream stop` / `status` | Stops the stream, or shows its addresses and state |
| `vpstream devices [enable\|disable DEVICE \| lock \| unlock]` | Who asked for the stream, and who gets in (a device is an address, or a browser's id) |
| `vpstream clients [set DEVICE... \| --all]` | Browser mode: where each browser is, and `--audio`, `--sub`, `--seek`, `--start-at`, `--name` for them |
| `vpstream alias NAME` | Changes the short name in the address |
| `vpstream revoke` | Takes back file access a stream that ended by itself left behind |
| `vpstream doctor` | Checks this host |
| `sudo vpstream init` / `uninstall` | Sets the service up, or removes it (`--purge` also the cache) |
| `sudo vpstream init --no-tailscale` / `--tailscale` | Puts the dashboard on the internet behind a password, or back on the tailnet |
| `sudo vpstream dash-pass` | Sets a new password for the public dashboard (logs every browser out; a stream goes on) |
| `vpstream dash-unblock` | Forgets the public dashboard's failed logins, to log in from a new browser |
| `vpstream dashboard-cert` | Prints the public dashboard's certificate authority, to install on your phone |
| `vpstream bench FILE` | Measures how fast this host copies, transcodes and burns in subtitles |
| `vpstream caps` | Serves a page that tests what a TV browser can play |
| `vpstream install ffmpeg` / `update ffmpeg` | Installs or updates a current static ffmpeg build |
| `vpstream selftest [--integration]` | Runs the tests shipped with the package |

## Streaming

```text
vpstream start --file /srv/media/film.mkv            # http://<ip>:1337/abc
vpstream start --dir /srv/media/show --alias show    # /show (index or playlist), /show/1, /show/2 ...
vpstream stop
```

- **Addresses:** `/ALIAS` plays a `--file` stream. For `--dir` it is an episode list in a
  browser and an M3U playlist in a player; videos are numbered in natural order of their names.
- **Who gets in:** `--policy open` (default; disable single addresses), `approval` (only those
  you approve; others wait, marked WL) or `locked` (only enabled ones). Change it live with
  `vpstream devices` or the dashboard. An address that tries 20 wrong paths is shut out.
- **Time limit:** `--ttl MINUTES`, default 180, 0 for none.
- **Your files:** the service reads videos as its own user. If it cannot, `start` gives it
  read access with an ACL entry for that user alone (needs the `acl` package and you owning
  the files); `stop` takes it back.
- **Seeking:** MKV files whose seek index has no audio entries (most of them) make VLC for
  Android resume by reading from the start. vpstream serves such a file from a copy with a full
  index, made once with mkvmerge (no re-encoding, about a minute per GB) and kept in
  `/var/cache/vpstream/index`; until it is ready the address says to try again. Without
  mkvtoolnix the file is served as it is.
- **Safety:** Caddy runs only while a stream does and asks the daemon about every request.
  Stop, the time limit and stopping the service close the port; after a crash the stream is
  taken up again, never after a reboot. The stream is plain HTTP.

## Browser mode

```text
vpstream track-info /srv/media/film.mkv                   # tracks, and how each is prepared
vpstream prepare --file /srv/media/film.mkv --sub 1,3     # base version and two subtitle tracks
vpstream start --file /srv/media/film.mkv --mode browser --audio eng --sub 1
```

- **Versions:** the base copies the video when browsers play it (H.264 8-bit, HEVC, AV1),
  else transcodes it to H.264 at its own size; every audio track becomes an AAC alternate.
  Plain text subtitles (SRT, mov_text, WebVTT) become soft WebVTT; image, ASS and SSA tracks
  get a burned-in version, one full encode each. Without `--sub`, every plain text track is
  prepared and nothing is burned in. A prepare runs in the service at low priority, one job
  after the other, and asks first when it would take longer than the film (`--yes`).
- **Start:** a missing base version is made by `start` itself when it is a copy (minutes);
  a transcode is asked about first. Until it is ready the address says how far it got.
- **Dashboard:** shows the plan of a prepare (what it makes, how long, the tracks it found)
  before it is queued, then its progress.
- **Player:** large buttons for a TV remote that works as a pointer: play, ±10 s, audio
  (each track, then Off), subtitles (Off, then each prepared one), episodes, next, full
  screen (also the `f` key on a computer) and hide. Other
  players get the HLS playlist at the same address.
- **Who gets in:** approval by default; a waiting browser shows a 4-digit code, which
  `vpstream devices` and the dashboard show next to its address. A browser gets a random
  cookie on its first page and is known by it, so an approved TV stays approved when its
  address changes, and a stranger behind the same address waits. Players such as VLC are known
  by their address. A stop forgets every cookie.
- **Viewers:** each browser's player reports where it is every 2 seconds. On the dashboard's
  Viewers tab (or `vpstream clients`) you tick one or more and set the audio, the subtitles, a
  seek now, where the next load starts, or a name. Reopening a video continues where that
  browser left off. A button pressed on the TV while a command is on its way wins.
- **Cache:** `/var/cache/vpstream/prepared`, kept until `vpstream prepared rm`. A changed
  file is prepared again; its old versions are listed as stale.

## Configuration

`/etc/vpstream/config.toml`: `port` (1337) and `host` (the address shown in URLs) under
`[stream]`, `port` (1338) under `[dashboard]`, and `copy_speed` and `encode_speed` under
`[prepare]` for the time estimates (`vpstream bench FILE` prints the values for this host), and
`folder` under `[media]`, the folder `vpstream ls` lists by default (`/srv/media`).
Restart the service after a change.

The dashboard listens on `127.0.0.1` and `init` publishes it with `tailscale serve` at
`https://<tailnet name>:1338/`. The stream port is opened and closed by a small root helper,
`vpstream-fw`, which only runs `ufw allow` / `ufw delete allow` for that one port.

## Public dashboard

`sudo vpstream init --no-tailscale` asks for a password (or takes `--dash-pass-file FILE`, or
`--dash-pass`, which stays in shell history) and puts the dashboard on the internet at
`https://<ip>:1338/`, through a Caddy of its own with a certificate from a local authority
(`tls internal`). Only a salted PBKDF2 verifier of the password is kept, in
`/etc/vpstream/dashboard.key` (readable by root and the service's group only).

- **Certificate:** what Caddy serves lasts 12 hours and renews itself, so `init` prints the
  authority's SHA-256 instead. Install the authority on your phone once, before the first
  login (`vpstream dashboard-cert > vpstream-ca.crt`; Android: Settings, Security, Install a
  certificate, CA certificate) and the browser trusts the dashboard with no warning. Each
  public address, IPv4 and IPv6, gets a certificate of its own.
- **Login:** challenge-response. The page derives a key from the password (WebCrypto, or plain
  JavaScript where the page has none) and answers a one-time nonce with an HMAC of it; the
  password never crosses the wire and a captured answer cannot be used again. That protects
  against someone who only listens. Before the authority is installed, someone who can change
  the traffic could serve a fake login page, so install it first.
- **Session:** a `__Host-` cookie (HTTPS only, HttpOnly, SameSite=Strict) for 12 hours, a CSRF
  token on every change, and Log out. A restart of the service logs everyone out.
- **Limits:** 5 wrong passwords from one address (one /64 for IPv6) in 15 minutes, or 50 from
  all addresses in an hour, and logins wait. A browser that logged in before keeps a signed
  device cookie (a year) and is counted on its own, so guessing from elsewhere does not lock
  it out. The count survives restarts; `vpstream dash-unblock` and `dash-pass` clear it.
- **Firewall:** the service opens the dashboard port in ufw while it serves it, and closes it
  on stop, crash or reboot. Open it in your provider's firewall too, if it has one.
- The command line keeps talking to the daemon over its socket, with no password.
  `sudo vpstream init --tailscale` goes back: the password is removed and the port closed.

## Development

```text
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest [--integration]
git config core.hooksPath .githooks
```

Pull request titles are Conventional Commits; releases are made from `main` by
python-semantic-release. hls.js (Apache-2.0) is bundled; `scripts/update-hlsjs.py` updates it.

## License

MIT
