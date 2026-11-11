# vpstream

On-demand video streaming from a VPS. The public port is open only while a stream runs, and
everything is controlled over SSH or a dashboard on your tailnet.

Status: early development. Streams a file or a folder as it is, for players such as VLC and
mpv (direct mode). Playing in a TV browser comes next.

## Install

Needs Linux, Python 3.12+, Caddy, ufw, Tailscale (with HTTPS certificates) and systemd.
ffmpeg is used by `bench` and `caps`; mkvtoolnix and the `acl` package are optional (see
below). `vpstream doctor` checks all of it and prints the fix for anything missing.

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
| `vpstream stop` / `status` | Stops the stream, or shows its addresses and state |
| `vpstream devices [enable\|disable ADDRESS \| lock \| unlock]` | Who asked for the stream, and who gets in |
| `vpstream alias NAME` | Changes the short name in the address |
| `vpstream revoke` | Takes back file access a stream that ended by itself left behind |
| `vpstream doctor` | Checks this host |
| `sudo vpstream init` / `uninstall` | Sets the service up, or removes it (`--purge` also the cache) |
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

## Configuration

`/etc/vpstream/config.toml`: `port` (1337) and `host` (the address shown in URLs) under
`[stream]`, `port` (1338) under `[dashboard]`. Restart the service after a change.

The dashboard listens on `127.0.0.1` and `init` publishes it with `tailscale serve` at
`https://<tailnet name>:1338/`. The stream port is opened and closed by a small root helper,
`vpstream-fw`, which only runs `ufw allow` / `ufw delete allow` for that one port.

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
