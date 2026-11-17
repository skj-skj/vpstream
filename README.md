# vpstream

On-demand video streaming from a VPS. The public port is open only while a stream runs, and
everything is controlled over SSH or a dashboard on your tailnet (or, if you choose, a public
dashboard over HTTPS behind a password).

Streams a file or a folder as it is, for players such as VLC and mpv (direct mode), or as
prepared HLS versions with a player page for TV browsers (browser mode), over plain HTTP or
HTTPS with a Let's Encrypt certificate for the address. Tested on Ubuntu Server 24.04.

## Install

Needs Linux with systemd, Python 3.12+, ufw, Caddy (2.10 or newer for HTTPS streams), and
Tailscale with HTTPS certificates (not needed for a public dashboard). ffmpeg is used by
browser mode, `bench` and `caps`; mkvtoolnix and the `acl` package are optional (see
Streaming). `vpstream doctor` checks all of it and prints the fix for anything missing.

```text
sudo apt install pipx ufw acl mkvtoolnix
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"
vpstream --version
vpstream doctor                 # what is missing, and the fix for each
vpstream install caddy          # the current Caddy (Ubuntu's 2.6.2 serves plain HTTP only)
vpstream install ffmpeg         # a current static ffmpeg, for browser mode
sudo vpstream init              # the service, its user, the firewall helper, the dashboard
# log out and in again: init added you to the vpstream group
vpstream doctor
vpstream selftest --integration
vpstream start --file /srv/media/film.mkv
```

The install must be system-wide: the service runs as its own user, and sudo runs the
firewall helper from that folder. The commands talk to the service through a socket that
only the `vpstream` group may use, which `init` adds you to; a login gets it only when it
starts (until then: `newgrp vpstream`).

`vpstream install caddy` and `vpstream install ffmpeg` ask first (`--yes` does not), and
`--tmp DIR` puts the download on another disk; `vpstream update ffmpeg` takes the same
`--yes` and `--tmp`.

## Upgrade

```text
vpstream stop
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx upgrade vpstream
sudo vpstream init
vpstream --version
vpstream doctor
```

`init` restarts the service on the new version, which ends a running stream: stop it first.
Everything is carried over: the configuration (`init` changes only the switches you give
it), the dashboard's password and its certificate authority (the phone keeps trusting it),
the stream's HTTPS certificate, and the prepared videos. The unit and the sudoers rule are
written again for the new version. Going back to an older version is not supported: a newer
one may write settings an older one rejects.

## Uninstall

```text
vpstream stop
vpstream revoke                 # file access a stream that ended by itself left behind
sudo vpstream uninstall --purge
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx uninstall vpstream
```

`uninstall` stops and removes the service, its user and group (and your membership), the
sudoers rule, the `tailscale serve` port it published, the ufw rules it opened, and
`/etc/vpstream` and `/var/lib/vpstream`, including the dashboard's password and the
stream's HTTPS certificate (a new one counts towards Let's Encrypt's 5 a week for the
address). It asks first (`--yes` does not). Without `--purge` the prepared videos in
`/var/cache/vpstream` stay; `--purge` removes them, and the ffmpeg `vpstream install ffmpeg`
put in `/opt/vpstream-ffmpeg` (an apt or snap ffmpeg it replaced is not put back:
`sudo apt install ffmpeg`). Left on the host: the apt packages you or `vpstream install` added (Caddy
and its repository, `/etc/apt/sources.list.d/caddy-stable.list` and
`/usr/share/keyrings/caddy-stable-archive-keyring.gpg`, the fonts), your videos, and any rule
in your provider's firewall. `pipx uninstall` removes vpstream's own
folder; pipx's (`/opt/pipx`, shared by everything installed this way) and root's pip cache
stay.

## Commands

| Command | What it does |
| --- | --- |
| `vpstream start --file F`, `vpstream start --dir D` | Starts a stream of one video, or of a folder's videos (`--mode`, `--alias`, `--ttl`, `--policy`, `--audio`, `--sub`, `--http`, `--yes`; see Streaming) |
| `vpstream ls [FOLDER]` | Lists a folder's videos (default `[media] folder`, `/srv/media`), their tracks and what is prepared |
| `vpstream track-info FILE` | Lists a file's tracks with the numbers `vpstream prepare --sub` and `vpstream start --audio` take |
| `vpstream prepare --file F`, `vpstream prepare --dir D` | Makes the versions browser mode plays (`--sub 1,3`, `eng`, `none`; `--yes`; `--no-wait` queues it and returns) |
| `vpstream prepared [rm PATH \| --stale \| --all \| cancel [PATH]]` | Lists, removes or cancels prepared versions |
| `vpstream stop` | Stops the stream and closes its port |
| `vpstream status [--json]` | Shows the stream's addresses and state, the dashboard's address and the certificate |
| `vpstream devices [enable\|disable DEVICE \| lock \| unlock]` | Who asked for the stream, and who gets in (a device is an address, or a browser's id) |
| `vpstream clients [set DEVICE... \| --all]` | Browser mode: where each browser is, and `--audio`, `--sub`, `--seek`, `--start-at` (with `--video` in a folder), `--name` for them |
| `vpstream alias NAME` | Changes the short name in the address |
| `vpstream revoke` | Takes back file access a stream that ended by itself left behind |
| `vpstream doctor` | Checks this host |
| `sudo vpstream init` | Sets the service up, or again after an upgrade or a change of the configuration |
| `sudo vpstream uninstall [--purge] [--yes]` | Removes it (see Uninstall) |
| `sudo vpstream init --no-tailscale`, `sudo vpstream init --tailscale` | Puts the dashboard on the internet behind a password, or back on the tailnet |
| `sudo vpstream init --local [--address IP]` | Serves the local network only, at a private address, with the dashboard there behind a password (see Local network mode); plain `init` goes back to the default |
| `sudo vpstream init --https`, `sudo vpstream init --no-https` | Serves streams over HTTPS with a Let's Encrypt certificate for the address, or over plain HTTP again |
| `sudo vpstream dash-pass [--dash-pass-file FILE]` | Sets a new password for the public dashboard (logs every browser out; a stream goes on) |
| `vpstream dash-unblock` | Forgets the public dashboard's failed logins, to log in from a new browser |
| `vpstream dashboard-cert` | Prints the public dashboard's certificate authority, to install on your phone |
| `vpstream bench FILE` | Measures how fast this host copies, transcodes and burns in subtitles (`--sample`, `--presets`, `--sub`, `--json`, `--tmp`, `--no-nice`) |
| `vpstream caps` | Serves a page that tests what a TV browser can play (`--host`, `--port`, `--ttl`, `--out`, `--clips`, `--regen`) |
| `vpstream install ffmpeg`, `vpstream update ffmpeg` | Installs or updates a current static ffmpeg build |
| `vpstream install caddy`, `vpstream update caddy` | Installs or updates the newest Caddy from Caddy's own apt repository (Debian, Ubuntu, Raspbian) |
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
  `vpstream devices` or the dashboard. An address that tries 20 wrong paths is shut out; for
  IPv6 that is its whole /64, and a /48 after 100.
- **Time limit:** `--ttl MINUTES`, default 180, 0 for none.
- **Your files:** the service reads videos as its own user. If it cannot, `start` gives it
  read access with an ACL entry for that user alone (needs the `acl` package and you owning
  the files); `stop` takes it back. A browser stream of a prepared video plays only what was
  prepared, so it needs no access: the dashboard can start it for any file you prepared.
- **Seeking:** MKV files whose seek index has no audio entries (most of them) make VLC for
  Android resume by reading from the start. vpstream serves such a file from a copy with a full
  index, made once with mkvmerge (no re-encoding, about a minute per GB) and kept in
  `/var/cache/vpstream/index`; until it is ready the address says to try again. Without
  mkvtoolnix the file is served as it is.
- **Safety:** Caddy runs only while a stream does and asks the daemon about every request.
  Stop, the time limit and stopping the service close the port; after a crash the stream is
  taken up again, never after a reboot. The stream is plain HTTP unless HTTPS is on (see
  HTTPS for streams).

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

`/etc/vpstream/config.toml`, written by `init` with every setting explained:

- `[stream]`: `port` (1337), `host` (the address shown in URLs; found by itself when left
  out) and `https` (false; set by `init --https` and `--no-https`).
- `[dashboard]`: `port` (1338) and `public` (false; set by `init --no-tailscale` and
  `--tailscale`).
- `[prepare]`: `copy_speed` and `encode_speed`, for the time estimates (`vpstream bench FILE`
  prints the values for this host).
- `[media]`: `folder`, what `vpstream ls` lists by default (`/srv/media`).
- `[acme]`: `ca`, `ca_root` and `port`, another certificate authority for trying HTTPS out
  (see HTTPS for streams).

An unknown setting or a wrong value is an error that names it; `vpstream doctor` shows it.
Restart the service after a change (`sudo systemctl restart vpstream`); after changing the
dashboard port, run `sudo vpstream init`, which publishes the new one.

The dashboard listens on `127.0.0.1` and `init` publishes it with `tailscale serve` at
`https://<tailnet name>:1338/`. The stream port is opened and closed by a small root helper,
`vpstream-fw`, which only runs `ufw allow` / `ufw delete allow` for that one port.

## Public dashboard

`sudo vpstream init --no-tailscale` asks for a password (or takes `--dash-pass-file FILE`, or
`--dash-pass`, which stays in shell history) and puts the dashboard on the internet at
`https://<ip>:1338/`, through a Caddy of its own with a certificate from an authority `init`
makes for this host (with `openssl`). Only a salted PBKDF2 verifier of the password is kept, in
`/etc/vpstream/dashboard.key` (readable by root and the service's group only).

- **Certificate:** what Caddy serves lasts 12 hours and renews itself, so `init` prints the
  authority's SHA-256 instead. Install the authority on your phone once, before the first
  login (`vpstream dashboard-cert > vpstream-ca.crt`; Android: Settings, Security, Install a
  certificate, CA certificate) and the browser trusts the dashboard with no warning. Each
  public address, IPv4 and IPv6, gets a certificate of its own.
- **The authority** is valid for this host's addresses only (X.509 name constraints): even with
  its key, nobody can make your phone trust it for another site. It is kept in
  `/etc/vpstream/dashboard-ca.{crt,key}` (the key for root and the service's group). When the
  host gets a new address, the dashboard stops serving it until `sudo vpstream init` makes a new
  authority; install that one and remove the old. Upgrading from 0.15.0 or earlier: run
  `sudo vpstream init`, install the new authority and remove "Caddy Local Authority" from the
  phone.
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
  `sudo vpstream init --tailscale` goes back: the password and the authority are removed and
  the port closed.

## Local network mode

For a home network, where every device is on the same Wi-Fi: no tailnet, no public address.
`sudo vpstream init --local` asks for a dashboard password (or takes `--dash-pass-file FILE`)
and sets the service up to serve the local network only. Plain `sudo vpstream init` is the
public mode, as always, and goes back to it.

- **Address:** the stream and the dashboard listen on this host's private address (the one it
  reaches the network from, or `--address IP`), never on every interface. Only private
  addresses are taken (192.168.x.x, 10.x.x.x, 172.16.x.x to 172.31.x.x, 169.254.x.x; fc00::/7
  and fe80::/10); a carrier's shared address (100.64.0.0/10) is refused. When the address
  goes, a running stream stops.
- **Dashboard:** at `http://<address>:1338/`, behind the password, over plain HTTP (cookies
  without Secure, under names of their own), and at `http://127.0.0.1:1338/` on this host
  without one, as on the tailnet. `init --local` lets 1338/tcp in from the private block of
  the address in ufw, and takes that back on the next plain `init` or `uninstall`.
- Use it only on a network you control, never on public Wi-Fi: anyone on the network can
  reach the stream and dashboard ports, and can see what crosses plain HTTP. `start` says so.

## HTTPS for streams

`sudo vpstream init --https` serves streams at `https://<ip>:1337/ALIAS` with a Let's Encrypt
certificate for the IPv4 address (the `shortlived` profile: 160 hours). It needs Caddy 2.10 or
newer, for ACME profiles; Ubuntu 24.04 has 2.6.2, so run `vpstream install caddy` first.

- **One certificate, kept:** a stream that starts uses the kept certificate when it lasts past
  the stream's end; otherwise a new one is requested then, before the stream starts, by a
  Caddy of its own that stops as soon as it has it or fails. Let's Encrypt allows 5 for the
  same address a week, so nothing is asked again while one lasts. Nothing changes while a
  stream runs, so an HTTPS stream runs at most 156 hours (`--ttl`). `init` keeps the
  certificate; `uninstall` removes it.
- **Port 80:** Let's Encrypt checks the address on port 80. The service opens it in ufw only
  while a certificate is requested, and closes it after (also on stop, crash or reboot); a
  rule that was there before stays. Let 80/tcp in your provider's firewall too.
- **When the request fails** (port 80 blocked, a rate limit), the stream does not start and
  says why. `vpstream start --http ...` (or "Plain HTTP this time" on the dashboard) starts that
  one stream over plain HTTP.
- **Cookie:** over HTTPS the approval cookie is `Secure`.
- **Trying it out:** `[acme] ca` sets another authority, such as Let's Encrypt's staging
  server or a local Pebble (with `ca_root` and `port`), so no real limit is spent.

## Troubleshooting

- **First look:** `vpstream doctor` (each problem with its fix), `vpstream status`, and
  `sudo journalctl -u vpstream -n 50 --no-pager`.
- **`no permission to use /run/vpstream/daemon.sock`:** log out and in again after the
  first `init`, which added you to the `vpstream` group.
- **`the vpstream daemon is not running`:** `systemctl status vpstream` and the journal say
  why; `sudo vpstream init` sets it up again.
- **`init` refuses the install:** a per-user `pipx install` cannot be used by the service;
  install it system-wide as shown in Install.
- **A stream starts but nobody gets in from outside:** open the stream port (1337/tcp) in
  your provider's firewall too; ufw is opened by vpstream only while a stream runs.
- **`start` cannot read a video:** it says who can give the service access and how
  (`setfacl`); a stream that ended on its own leaves that access behind until
  `vpstream revoke`.
- **HTTPS: `no HTTPS certificate for the stream`:** most often port 80 is blocked in the
  provider's firewall, or Let's Encrypt's limit is reached (5 certificates a week for an
  address, 5 failed checks an hour). The request's log is
  `/var/lib/vpstream/caddy/certificate/caddy.log`. `vpstream start --http` meanwhile.
- **`GitHub's API limit for this IP is used up`** (`vpstream install ffmpeg`): wait an hour,
  or set `GITHUB_TOKEN` to a GitHub token, which is sent to GitHub's API only.
- **Public dashboard, logins wait:** `vpstream dash-unblock` forgets the failed logins.
- **Port 1337 still open after something went wrong:** `sudo systemctl restart vpstream`
  closes it; the service also closes it after a crash or a reboot.

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
