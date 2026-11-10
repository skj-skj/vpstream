# vpstream

On-demand video streaming from a VPS. The public port opens only while a stream runs, and
everything is controlled over SSH or a private dashboard.

Status: early development. Streaming itself is not here yet. This release has the measuring
tools and the service skeleton:

| Command | What it does |
| --- | --- |
| `vpstream doctor` | Checks this host for everything vpstream needs |
| `sudo vpstream init` / `uninstall` | Sets the service up on this host, and removes it again |
| `vpstream status` | Shows what the running daemon is doing |
| `vpstream bench FILE` | Measures how fast this host copies, transcodes and burns in subtitles for a file |
| `vpstream caps` | Serves a test page that checks what a TV browser can play |
| `vpstream selftest` | Runs the tests shipped with the package (`--integration` uses the real ffmpeg) |
| `vpstream install ffmpeg` | Installs the newest stable ffmpeg build; `vpstream update ffmpeg` updates it |

## Requirements

- Linux, Python 3.12 or newer
- ffmpeg and ffprobe on `PATH` (not bundled), with libx264; libx265 and an AV1 encoder
  (libsvtav1 or libaom-av1) for the full `caps` test set. `vpstream install ffmpeg` installs a
  build with all of them.
- Optional: mkvtoolnix (`sudo apt install mkvtoolnix`), only for WebVTT subtitle tracks written
  by mkvmerge, which ffmpeg cannot read; vpstream says when a file needs it

## Install

```text
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"
vpstream --version
vpstream doctor
sudo vpstream init
```

The dev extra adds pytest for `vpstream selftest`. The install is for the whole system and
owned by root, because the service runs as its own user and its firewall helper runs as root;
a per-user `pipx install` cannot be used for the service. Upgrade with the same prefix:

```text
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx upgrade vpstream
sudo vpstream init               # restarts the service on the new version
```

## `sudo vpstream init` and `sudo vpstream uninstall`

`init` sets the service up and is safe to run again, for example after an upgrade:

- a service user and group, `vpstream`, with no login and no home
- `/etc/vpstream/config.toml` (kept if it exists), a state folder and a cache folder
- a sudoers rule that lets the service user run `vpstream-fw open` and `vpstream-fw close` as
  root and nothing else; it is checked with `visudo` before and after it is installed
- the dashboard, published on your tailnet with `tailscale serve`: HTTPS on the dashboard
  port of this host's tailnet name, proxied to `127.0.0.1`. No firewall rule is added for it
- the systemd service, enabled and started; it closes the stream port after a stop or a crash
- the user who ran `sudo` is added to the `vpstream` group, to use `vpstream status` without
  sudo (log out and in once)

`uninstall` removes what `init` created and nothing else: a user, a group membership or a
served port that was there before stays, and on a host that was never set up no ufw rule is
touched. The configuration and state are removed; prepared
videos are kept unless `--purge` is given. It asks first; `--yes` skips the question.

Neither command ever enables, disables, resets or reloads ufw, or touches any ufw rule other
than the stream port. Neither resets `tailscale serve`: only the dashboard port is added or
turned off, and a port that already serves something else is refused.

`init` needs Tailscale running, with HTTPS certificates enabled for the tailnet. It refuses a
dashboard port that another program on this host already listens on.

## `vpstream doctor`

Checks this host for everything vpstream needs and lists all of it in one report: the Python
version, ffmpeg, ffprobe, Caddy, ufw, Tailscale, systemd and sudo, whether ufw is enabled,
Tailscale is connected and the tailnet issues HTTPS certificates, the fonts burned-in subtitles fall back to, the configuration file,
and where vpstream itself is installed. Each problem comes with the command that fixes it.
The exit code is 1 when something required is missing. Nothing is changed and no root is
needed.

A per-user `pipx install` is reported as a problem, with the system-wide install command as
the fix (see Install); remove the per-user copy afterwards with `pipx uninstall vpstream`.

Settings are read from `/etc/vpstream/config.toml` when it exists: `port` under `[stream]`
(default 1337) and `port` under `[dashboard]` (default 1338).

## `vpstream daemon` and `vpstream status`

One daemon does the work, and the command line and the dashboard both talk to it.
`vpstream daemon` runs it in the foreground; normally the service does that. It refuses to
start while another daemon is listening on the same socket.

- **Command line:** `vpstream status` asks the daemon over a unix socket,
  `/run/vpstream/daemon.sock`, which only the service user and its group can use.
  `--json` prints the raw answer.
- **Dashboard port:** bound to `127.0.0.1` only, never to every interface. `vpstream init`
  publishes it on your tailnet with `tailscale serve`, so the dashboard is
  `https://<this host's tailnet name>:1338/`, reachable only from your own devices.
  `vpstream status` shows that address, or says that the dashboard is not published.

## The firewall helper, `vpstream-fw`

The stream port is open in ufw only while a stream runs. The daemon has no privileges, so a
small helper does it as root: `vpstream-fw open` and `vpstream-fw close`, nothing else.

- **One port:** the helper takes no port. It reads `port` under `[stream]` from the root-owned
  `/etc/vpstream/config.toml` and refuses a file that other users can write, so its caller
  cannot make it touch any other port.
- **What it opened, it closes:** `open` writes the port down in `/etc/vpstream/open-ports`
  before it opens it, and `close` closes every port written there as well as the configured
  one. A port therefore stays closable after the configuration was changed, or broken, while
  it was open.
- **Two ufw commands:** `ufw allow PORT/tcp` and `ufw delete allow PORT/tcp`. It never enables,
  disables, resets or reloads ufw and never touches another rule.
- **After a crash or reboot:** a ufw rule stays on disk, so when the daemon starts it closes
  the stream port unless a running stream is recorded. `vpstream status` shows the port's
  state, and a warning when the helper could not be run.

## `vpstream install ffmpeg` and `vpstream update ffmpeg`

Distribution packages of ffmpeg are often several versions old. `vpstream install ffmpeg`
installs the newest stable build from [FFmpeg-Builds](https://github.com/FFmpeg-Builds/FFmpeg-Builds),
the Linux build that ffmpeg.org links to: the highest release branch (9.0 now, 10.0 once it
exists), never the development snapshot. `vpstream update ffmpeg` replaces it with a newer
build when there is one, including a new major version.

- **Where:** ffmpeg and ffprobe go to `/opt/vpstream-ffmpeg`, linked from `/usr/local/bin`,
  which comes before `/usr/bin` on `PATH`. System changes run through `sudo`.
- **Checked first:** the download is checked against its published SHA-256, and the new ffmpeg
  is run and must have libx264, libx265, an AV1 encoder and the zscale, tonemap, subtitles,
  overlay and scale2ref filters. If anything fails, nothing on the system is changed.
- **Libraries:** the builds need glibc 2.28 or newer (Ubuntu 20.04 and later) on x86-64 or
  ARM64. On apt systems, the packages they use are installed when missing: `libgcc-s1`,
  `fontconfig`, and the fonts burned-in subtitles fall back to (`fonts-dejavu-core`,
  `fonts-liberation`, and `fonts-noto-cjk` for Chinese, Japanese and Korean, about 90 MB).
  Running `vpstream install ffmpeg` again adds any that are missing.
- **An existing ffmpeg:** you are asked first, `This will uninstall the existing ffmpeg (...).
  Do you want to proceed? [y/N]`; the question lists every package apt would remove with it.
  An apt or snap ffmpeg is removed after the new one is in place. An ffmpeg from elsewhere is
  never removed: vpstream asks you to remove it yourself. `--yes` skips the question.
- **Uninstall:** `sudo rm -r /opt/vpstream-ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe`.

## `vpstream bench FILE`

Encodes a sample of the file (120 seconds from about 10% in, `--sample` to change) at low CPU
and I/O priority, and reports the speed of each step as a multiple of real time, with the
estimated time for the whole file:

- **copy:** the video copied into fMP4 HLS, audio converted to AAC
- **transcode:** H.264 at the source resolution, for each x264 preset in `--presets`
  (default `veryfast,medium`); HDR sources are tone mapped
- **burn-in:** the first image, ASS or SSA subtitle track (or `--sub N`) drawn into the video;
  for ASS tracks, font substitutions reported by libass are listed

`--json PATH` saves the results.

## `vpstream caps`

Generates short test clips (H.264, HEVC and AV1 in 8 and 10 bit, two audio tracks, WebVTT),
then serves a page on port 1337 (`--port`) that a TV browser opens at
`http://<server-ip>:1337/caps`. The page is driven with the arrow keys and OK. It tests MP4 and
HLS playback (hls.js and native), a subtitle overlay, audio track switching, download speed,
and records which remote keys reach the page. Results are sent back and saved to
`caps-results-*.json`. The server stops after 60 minutes (`--ttl`).

Clips are cached under `~/.cache/vpstream`. `--clips DIR` must be a new or empty folder, or one
that `vpstream caps` made before; any other folder is refused, never cleared. `--regen` makes
the clips again.

The port must be reachable: open it in your firewall for the test and close it afterwards.

## Development

```text
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                  # unit tests
.venv/bin/pytest --integration    # also the tests that run ffmpeg
git config core.hooksPath .githooks
```

Pull requests need a Conventional Commit title (`feat: ...`, `fix: ...`). Releases are made
from `main` by python-semantic-release.

hls.js (Apache-2.0) is bundled in `src/vpstream/assets`, with its version in `hls.js.version`.
`python scripts/update-hlsjs.py` updates it from npm (checked against npm's sha512 integrity;
`--version X` for a given release, `--check` to only compare). A weekly workflow runs the same
script and opens a pull request when a new release is out.

## License

MIT
