# vpstream

On-demand video streaming from a VPS. The public port opens only while a stream runs, and
everything is controlled over SSH or a private dashboard.

Status: early development. This release contains only the measuring tools:

| Command | What it does |
| --- | --- |
| `vpstream bench FILE` | Measures how fast this host copies, transcodes and burns in subtitles for a file |
| `vpstream caps` | Serves a test page that checks what a TV browser can play |
| `vpstream selftest` | Runs the tests shipped with the package (`--integration` uses the real ffmpeg) |

## Requirements

- Linux, Python 3.12 or newer
- ffmpeg and ffprobe on `PATH` (not bundled), with libx264; libx265 and an AV1 encoder
  (libsvtav1 or libaom-av1) for the full `caps` test set
- Optional: mkvtoolnix (`sudo apt install mkvtoolnix`), only for WebVTT subtitle tracks written
  by mkvmerge, which ffmpeg cannot read; vpstream says when a file needs it

## Install

```text
pipx install "vpstream[dev]"     # the dev extra adds pytest for `vpstream selftest`
vpstream --version
vpstream selftest --integration
```

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
