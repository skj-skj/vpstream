# vpstream

On-demand video streaming from a VPS. The public port opens only while a stream runs, and
everything is controlled over SSH or a private dashboard.

Status: early development. This release contains only the measuring tools:

| Command | What it does |
| --- | --- |
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
pipx install "vpstream[dev]"     # the dev extra adds pytest for `vpstream selftest`
vpstream --version
vpstream selftest --integration
```

## `vpstream doctor`

Checks this host for everything vpstream needs and lists all of it in one report: the Python
version, ffmpeg, ffprobe, Caddy, ufw, Tailscale, systemd and sudo, whether ufw is enabled and
Tailscale is connected, the fonts burned-in subtitles fall back to, the configuration file,
and where vpstream itself is installed. Each problem comes with the command that fixes it.
The exit code is 1 when something required is missing. Nothing is changed and no root is
needed.

The service runs as its own user, with a small root helper for the firewall, so it needs a
system-wide install that root owns. A per-user `pipx install` is reported as a problem:

```text
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"
pipx uninstall vpstream          # the per-user copy, if there is one
```

Settings are read from `/etc/vpstream/config.toml` when it exists: `port` under `[stream]`
(default 1337) and `port` under `[dashboard]` (default 1338).

## `vpstream daemon` and `vpstream status`

One daemon does the work, and the command line and the dashboard both talk to it.
`vpstream daemon` runs it in the foreground; normally the service does that.

- **Command line:** `vpstream status` asks the daemon over a unix socket,
  `/run/vpstream/daemon.sock`, which only the service user and its group can use.
  `--json` prints the raw answer.
- **Dashboard port:** bound to this host's Tailscale address only (from `tailscale ip -4`),
  never to every interface. While Tailscale has no address the daemon keeps running and tries
  again every 10 seconds; `vpstream status` says why the dashboard is not available.

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
