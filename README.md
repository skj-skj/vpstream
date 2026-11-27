# vpstream

Stream a video file or a folder on demand, to VLC and other players (direct mode) or to a
TV's web browser (browser mode). The stream's port is open only while a stream runs.

| Where | How it serves | Controlled from |
| --- | --- | --- |
| [Linux server (VPS)](#linux-server-vps) | the internet, or your home network | SSH, and a dashboard on your tailnet |
| [Android, in Termux](#android-termux) | your home network only | the terminal, and a dashboard on the phone's Wi-Fi |
| [Windows](#windows) | your home network only | a desktop window |

> **Termux and Windows serve your private local network only** (192.168.x.x, 10.x.x.x,
> 172.16-31.x.x). Use them on your own Wi-Fi, never on public Wi-Fi: anyone on the network
> can reach the stream.

## Contents

- [Linux server (VPS)](#linux-server-vps): [install](#install), [update](#update),
  [uninstall](#uninstall)
- [Android (Termux)](#android-termux)
- [Windows](#windows)
- [Streaming (Linux and Termux)](#streaming-linux-and-termux): [direct mode](#direct-mode),
  [browser mode](#browser-mode), [who may watch](#who-may-watch)
- [Commands](#commands)
- [More on Linux](#more-on-linux): [configuration](#configuration),
  [public dashboard](#public-dashboard), [local network mode](#local-network-mode),
  [HTTPS for streams](#https-for-streams), [watch together](#watch-together-over-the-internet)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Linux server (VPS)

Tested on Ubuntu Server 24.04. Needs systemd, Python 3.12+, ufw, Caddy (2.10+ for HTTPS
streams) and Tailscale (not needed for a public dashboard). `vpstream doctor` checks all of it
and prints the fix for anything missing.

### Install

```text
sudo apt install pipx ufw acl mkvtoolnix
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"
vpstream install caddy          # the current Caddy (Ubuntu's 2.6.2 serves plain HTTP only)
vpstream install ffmpeg         # a current ffmpeg, for browser mode
sudo vpstream init              # the service, its user, the firewall helper, the dashboard
# log out and in again: init added you to the vpstream group
vpstream doctor
vpstream selftest --integration
```

The install must be system-wide: the service runs as its own user. The dashboard is at
`https://<tailnet name>:1338/`.

### Update

```text
vpstream stop
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx upgrade vpstream
sudo vpstream init              # restarts the service on the new version
vpstream doctor
```

Settings, the dashboard's password and certificate, the stream's HTTPS certificate and the
prepared videos are kept. `vpstream update caddy` and `vpstream update ffmpeg` update those.

### Uninstall

```text
vpstream stop
vpstream revoke                 # file access a stream that ended by itself left behind
sudo vpstream uninstall --purge # --purge: also the prepared videos and vpstream's ffmpeg
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx uninstall vpstream
```

It removes the service, its user, the firewall rules it opened and `/etc/vpstream` and
`/var/lib/vpstream`. Your videos, the apt packages and your provider's firewall rules stay.

## Android (Termux)

The phone streams to a TV or another phone on the same Wi-Fi. No root, no service.

**Install and set up**

```text
pkg install python ffmpeg caddy mkvtoolnix
termux-setup-storage              # lets Termux read the phone's videos
pip install "vpstream[dev]"
vpstream init                     # the configuration, and what is left to do
vpstream dash-pass                # the dashboard's password
vpstream up                       # the daemon in the background; keeps the phone awake
vpstream doctor
```

**Update**

```text
vpstream down
pip install --upgrade "vpstream[dev]"
vpstream up
```

**Stop:** `vpstream down` stops the daemon and lets the phone sleep.

- **Private network only:** vpstream listens on the phone's Wi-Fi address, which must be
  private. Android has no firewall, so use your own Wi-Fi only. Turning the Wi-Fi off stops
  a running stream.
- **Dashboard:** `http://<phone's address>:1338/`, behind the password.
- **Files:** configuration `~/.config/vpstream/config.toml`, log
  `~/.local/state/vpstream/daemon.log`, prepared videos `~/.cache/vpstream/`.
- **If Android stops Termux:** run `vpstream up` again; a running stream comes back. If a
  stream stops by itself on Android 12+, turn off "child process restrictions" in the
  developer options (or see the Termux wiki on "phantom processes").
- **A phone serves one or two devices.** Transcoding is slow: prepare ahead, or use direct
  mode.

Streaming works as on Linux: see [Streaming](#streaming-linux-and-termux).

## Windows

A desktop window that streams, prepares videos for browsers and installs the programs it
needs. Windows 10 or 11, 64-bit.

**Install**

1. Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/)
   (tick "Add python.exe to PATH").
2. In a terminal:

   ```text
   py -m pip install vpstream
   py -m vpstream
   ```

   Windows asks to let vpstream make changes (it adds a firewall rule while a stream runs):
   choose Yes. A **vpstream** icon is put on the desktop; open it from there next time.
3. On first open, the window offers to install ffmpeg, Caddy and MKVToolNix. Press
   **Install**.

**Update:** close the window, then `py -m pip install --upgrade vpstream`. The programs it
uses are updated from the window's **Tools** tab.

**Uninstall:** `py -m vpstream windows-uninstall` (the programs it installed and the desktop
icon), then `py -m pip uninstall vpstream`.

**The window**

- **Stream:** pick a file or a folder, choose VLC or a web browser/TV and who may watch, then
  Start. The address to open on the TV or phone is shown big, with Copy. Devices are listed
  as they connect; select one to Let in, Keep out or Rename.
- **Prepare for browsers:** pick a video and its subtitles; Prepare queues it. The queue
  shows each video's progress; the prepared ones can be streamed or removed.
- **Tools:** which ffmpeg, Caddy and MKVToolNix are used, and vpstream's own copies:
  install, update to the latest release (checked against its published SHA-256) or remove.

**Good to know**

- **Private network only:** the stream listens on this PC's private address. The window warns
  when Windows treats your Wi-Fi as Public (phones cannot reach the PC then) and offers to
  make it private.
- **Files:** settings in `%APPDATA%\vpstream`; programs, prepared videos and the log
  (`vpstream.log`, with Open log in the window) in `%LOCALAPPDATA%\vpstream`.
- `py -m vpstream doctor` checks the install.

## Streaming (Linux and Termux)

### Direct mode

For VLC, mpv and other players. The file is served as it is.

```text
vpstream start --file /srv/media/film.mkv            # http://<ip>:1337/abc
vpstream start --dir /srv/media/show --alias show    # /show (a list), /show/1, /show/2 ...
vpstream status
vpstream stop
```

- `--ttl MINUTES`: stops by itself after this long (default 180, 0 for never).
- For a folder, `/ALIAS` is an episode list in a browser and a playlist in a player.
- MKV files are given a full seek index (mkvmerge, once, no re-encoding), so VLC for Android
  resumes them quickly.

### Browser mode

For a web browser (a phone, a laptop or a TV): the video is prepared as HLS first, with a player
page of its own.

```text
vpstream track-info /srv/media/film.mkv                    # its tracks, with their numbers
vpstream prepare --file /srv/media/film.mkv --sub 1,3      # the base version and 2 subtitles
vpstream prepare --dir /srv/media/show --sub eng           # a folder; English subtitles
vpstream prepared                                          # what is prepared
vpstream start --file /srv/media/film.mkv --mode browser --audio eng --sub 1
```

- **Prepare:** copies the video when browsers can play it, else transcodes it to H.264. Text
  subtitles become soft subtitles; image and ASS subtitles are burned in (one encode each). It
  asks first when it would take longer than the film (`--yes`); `--no-wait` queues it and
  returns.
- **Player:** touch controls by default. A tap in the middle plays or pauses, a double tap on
  the left or right steps 10 s back or on (each tap after it 10 s more), and the bar is
  dragged to seek. The subtitles button opens a sheet with the audio, the subtitles and their
  size (extra small to extra large). The bottom row has full screen and, on a phone, rotate.
  Full screen turns the screen to the video's shape where the browser allows it (Android).
  On an iPhone it is the system's own video player, and the soft subtitles go with it; there
  rotate turns the page on the screen, as iOS lets no page lock the screen.
- **The old player** (`--ui old`): large buttons for a TV's remote. Switch a running stream
  with `vpstream ui old` / `vpstream ui new`, or on the dashboard; open pages load again with it.
- `vpstream clients` (or the dashboard's Viewers tab) changes the audio, subtitles, subtitle
  size or position of a TV from afar. The subtitle size is each device's own, also when
  watching together, and is there for soft subtitles only.
- **Remove:** `vpstream prepared rm PATH`, `--stale` or `--all`.

#### Watch together

Several devices watch at the same place, as one group:

```text
vpstream start --file /srv/media/film.mkv --mode browser --together
vpstream together                 # where the group is, and how far each device is off it
vpstream together play            # also: pause, seek 12:30, skip -10, audio 2, sub 1-soft, video 3
```

- You let each device in (the policy is `approval`); the group starts paused at 0:00.
- Play, pause, a seek, audio, subtitles and (in a folder) another video, pressed on any
  device, apply to everyone. The first press applies at once; presses in the 2 seconds after
  it are settled when those end, the last of each kind winning.
- A pause pauses the device it was pressed on at once, before the others hear of it.
- A device that joins, or comes back, starts where the group is, and its buttons work only
  10 seconds after it got there (Full screen, Hide and Sync always do).
- A device keeps close to the group by itself. One more than 2 seconds off (its video waits
  for data on a slow link) is out of sync: it plays on where it is and shows **Sync**, which
  puts it at the group's place, behind or ahead; Sync works in the cooldown too. Any change
  for everyone puts every device at the group's place. The group pauses at the end of a video.
- With the new player, each viewer can give themselves a name (the face at the top). Every
  device then says who paused, seeked or changed the audio or subtitles ("Asha paused"), so
  nobody takes it for a fault. Full screen, rotation and the subtitle size stay each device's own.
- The dashboard's Start form has a Watch together switch; its Viewers tab shows the group
  and changes it for everyone.

### Who may watch

`--policy open` (anyone; the default in direct mode), `approval` (you let each one in; the
default in browser mode, where a waiting TV shows a 4-digit code) or `locked` (only those let
in). `vpstream devices` lists them and lets them in or keeps them out, live:

```text
vpstream devices                 # who asked, and their state
vpstream devices enable DEVICE   # let in (disable: keep out)
vpstream devices lock            # nobody new from now on
```

## Commands

| Command | What it does |
| --- | --- |
| `vpstream start --file F` / `--dir D` | Starts a stream (`--mode browser`, `--alias`, `--ttl`, `--policy`, `--audio`, `--sub`, `--http`, `--together`, `--ui`) |
| `vpstream stop`, `vpstream status [--json]` | Stops the stream; shows its addresses and state |
| `vpstream ls [FOLDER]` | A folder's videos, their tracks and what is prepared |
| `vpstream track-info FILE` | A file's tracks, with the numbers prepare and start take for subtitles and audio |
| `vpstream prepare --file F` / `--dir D` | Prepares for browser mode (`--sub 1,3` / `eng` / `none`, `--yes`, `--no-wait`) |
| `vpstream prepared [rm PATH \| --stale \| --all \| cancel]` | Lists, removes or cancels prepared versions |
| `vpstream devices [enable\|disable DEVICE \| lock \| unlock]` | Who asked for the stream, and who gets in |
| `vpstream clients [set DEVICE... \| --all]` | Browser mode: what each TV plays; `--audio`, `--sub`, `--seek`, `--sub-size`, `--start-at` (`--video` in a folder), `--name` |
| `vpstream ui new` / `vpstream ui old` | Browser mode: the running stream's player, touch controls or large buttons for a TV remote |
| `vpstream together [status \| play \| pause \| seek T \| skip S \| audio N \| sub S \| video N]` | Watching together: the group's state, or a change for everyone |
| `vpstream alias NAME` | Changes the name in the address |
| `vpstream doctor` | Checks this machine, and says how to fix what is missing |
| `vpstream up`, `vpstream down` | Termux: starts or stops the daemon |
| `sudo vpstream init [--local \| --https \| --no-tailscale]` | Linux: sets the service up (see [More on Linux](#more-on-linux)) |
| `sudo vpstream dash-pass [--dash-pass-file FILE]` | Linux: a new dashboard password (`--dash-pass` takes it on the command line, and leaves it in shell history) |
| `sudo vpstream uninstall [--purge]` | Linux: removes it |
| `vpstream install ffmpeg\|caddy [--yes] [--tmp DIR]`, `vpstream update ffmpeg\|caddy` | Linux: installs or updates them; they ask first unless `--yes`, and `--tmp DIR` puts the download on another disk |
| `vpstream windows-install [PROGRAM...] [--yes]`, `vpstream windows-uninstall [--yes]` | Windows: the programs it needs, and the desktop icon |
| `vpstream bench FILE` | How fast this machine copies, transcodes and burns in (`--sample`, `--presets`, `--sub`, `--json`, `--tmp`, `--no-nice`) |
| `vpstream caps` | A page that tests what a TV browser can play (`--host`, `--port`, `--ttl`, `--out`, `--clips`, `--regen`) |
| `vpstream selftest [--integration]` | Runs the tests shipped with the package |

## More on Linux

### Configuration

`/etc/vpstream/config.toml`, written by `init` with every setting explained: stream port
(1337), dashboard port (1338), the address shown, HTTPS, prepare speeds (`vpstream bench FILE`
measures them) and the media folder. `vpstream doctor` names a wrong setting. After a change:
`sudo systemctl restart vpstream` (after a dashboard port change: `sudo vpstream init`).

### Public dashboard

`sudo vpstream init --no-tailscale` puts the dashboard on the internet at `https://<ip>:1338/`
behind a password (asked for, or `--dash-pass-file FILE`; `--dash-pass` leaves it in shell
history), with a certificate authority made
for this host's addresses only.

- Install the authority on your phone first: `vpstream dashboard-cert > vpstream-ca.crt`
  (Android: Settings, Security, Install a certificate, CA certificate).
- The password never crosses the network (challenge-response). Logins from one address wait
  after 5 wrong passwords; `vpstream dash-unblock` clears that, `sudo vpstream dash-pass` sets
  a new password.
- `sudo vpstream init --tailscale` goes back to the tailnet.

### Local network mode

`sudo vpstream init --local [--address IP]` serves your home network only, at a private
address, with the dashboard at `http://<address>:1338/` behind a password. Plain
`sudo vpstream init` goes back.

### HTTPS for streams

`sudo vpstream init --https` serves streams at `https://<ip>:1337/ALIAS` with a Let's Encrypt
certificate for the address (Caddy 2.10+: `vpstream install caddy`). Port 80 is opened only
while a certificate is asked for; let 80/tcp in your provider's firewall too. A certificate is
kept while it lasts (Let's Encrypt allows 5 a week per address). `vpstream start --http` starts
one stream over plain HTTP. `sudo vpstream init --no-https` turns it off.

### Watch together over the internet

[Watch together](#watch-together) works the same for viewers anywhere, over HTTPS too.

- A device keeps within a few tenths of a second of the group over trips of up to 300 ms.
  One that joins seeks to the group's place, so its buttons work about 10 seconds after
  the video loads.
- The Viewers tab shows how far each device is off, as the device itself measures it, marks
  one more than 2 seconds off as out of sync, and shows the seconds left of its cooldown.
- A link slower than the video (look at its peak bitrate, not its average) makes a device
  stop to load: it falls out of sync, and Sync brings it back once it has loaded.
- Up to 24 pages (3 per device) wait for the group's changes at once. More still follow it,
  a few seconds late.

## Troubleshooting

- **First look:** `vpstream doctor`, `vpstream status`, and on Linux
  `sudo journalctl -u vpstream -n 50 --no-pager` (Windows: Open log in the window).
- **`no permission to use /run/vpstream/daemon.sock`:** log out and in again after the first
  `init`.
- **Nobody gets in from outside (VPS):** open 1337/tcp in your provider's firewall too.
- **`start` cannot read a video (Linux):** it says how to give the service access;
  `vpstream revoke` takes back what a stream that ended by itself left.
- **No HTTPS certificate:** port 80 is blocked, or Let's Encrypt's limit is reached; the log
  is `/var/lib/vpstream/caddy/certificate/caddy.log`. `vpstream start --http` meanwhile.
- **`GitHub's API limit for this IP is used up`:** wait an hour, or set `GITHUB_TOKEN`.
- **Windows: phones cannot reach the PC:** the window's warning about a Public network; press
  Make it private.

## Development

```text
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -n auto [--integration]    # -n auto: on every core
git config core.hooksPath .githooks
```

Pull request titles are Conventional Commits; releases are made from `main` by
python-semantic-release. hls.js (Apache-2.0) is bundled; `scripts/update-hlsjs.py` updates it.

## License

MIT
