# CHANGELOG

<!-- version list -->

## v0.11.0 (2026-11-12)

### Features

- Client control in browser mode: approval cookie, viewer reports and commands
  ([#20](https://github.com/skj-skj/vpstream/pull/20),
  [`367b270`](https://github.com/skj-skj/vpstream/commit/367b270848d3a9f9438d632f06ef3826916b94fd))


## v0.10.1 (2026-11-12)

### Bug Fixes

- A prepared video whose file cannot be looked at is kept, not stale
  ([#19](https://github.com/skj-skj/vpstream/pull/19),
  [`6389c4f`](https://github.com/skj-skj/vpstream/commit/6389c4fe0939a9661c025a63e39fe9dc72cfc17e))


## v0.10.0 (2026-11-11)

### Features

- A dashboard that works on a phone: tabs, cards, progress and codes
  ([#18](https://github.com/skj-skj/vpstream/pull/18),
  [`06731cb`](https://github.com/skj-skj/vpstream/commit/06731cbe1c9a6ded8bba0d020615becb1649d390))


## v0.9.1 (2026-11-11)

### Bug Fixes

- Name the init segment of a burned-in version, so browsers can load it
  ([#17](https://github.com/skj-skj/vpstream/pull/17),
  [`c952b74`](https://github.com/skj-skj/vpstream/commit/c952b74e5512a5f1e6158bc33619ce69074239fb))


## v0.9.0 (2026-11-11)

### Features

- Browser mode with prepared HLS versions, prepare, ls and track-info
  ([#16](https://github.com/skj-skj/vpstream/pull/16),
  [`a0176ae`](https://github.com/skj-skj/vpstream/commit/a0176ae2bc1225fdcc63f5fb7305be10317d6704))


## v0.8.4 (2026-11-11)

### Bug Fixes

- Serve MKV files with a full seek index, so VLC resumes them quickly
  ([#15](https://github.com/skj-skj/vpstream/pull/15),
  [`3749019`](https://github.com/skj-skj/vpstream/commit/3749019b7316f31b0a596ed7a4b81f39e1894183))


## v0.8.3 (2026-11-11)

### Bug Fixes

- Count the bytes each viewer got from the kernel, not only from Caddy's log
  ([#14](https://github.com/skj-skj/vpstream/pull/14),
  [`8605f3c`](https://github.com/skj-skj/vpstream/commit/8605f3c8e7383fed5fa60e1e2fdab41c467295c3))


## v0.8.2 (2026-11-11)

### Bug Fixes

- Remove the ACL mask that setfacl leaves when the last entry is taken back
  ([#13](https://github.com/skj-skj/vpstream/pull/13),
  [`6663dc3`](https://github.com/skj-skj/vpstream/commit/6663dc384699b79e8e06a862e96bc9ff338ab63d))


## v0.8.1 (2026-11-11)

### Bug Fixes

- Vpstream start gives the service access to the caller's own videos
  ([#12](https://github.com/skj-skj/vpstream/pull/12),
  [`47935f5`](https://github.com/skj-skj/vpstream/commit/47935f5f80ebea18cf4da555d5a0424d3620e572))


## v0.8.0 (2026-11-11)

### Features

- Stream a file or a folder in direct mode, behind the gate
  ([#11](https://github.com/skj-skj/vpstream/pull/11),
  [`48597c0`](https://github.com/skj-skj/vpstream/commit/48597c0825d6852cf7720bde54fa7f772fcc5e97))


## v0.7.1 (2026-11-10)

### Bug Fixes

- Close what was opened, refuse a second daemon, and the other review findings
  ([#10](https://github.com/skj-skj/vpstream/pull/10),
  [`6fc696c`](https://github.com/skj-skj/vpstream/commit/6fc696cb1ff0cd475eeaa34db58a47077f750cc6))


## v0.7.0 (2026-11-10)

### Features

- Publish the dashboard with tailscale serve instead of a ufw rule
  ([#9](https://github.com/skj-skj/vpstream/pull/9),
  [`cff1037`](https://github.com/skj-skj/vpstream/commit/cff1037c07ad0680c542830f54970ff44a5812d0))


## v0.6.0 (2026-11-10)

### Features

- Add vpstream init and uninstall ([#8](https://github.com/skj-skj/vpstream/pull/8),
  [`e33dab4`](https://github.com/skj-skj/vpstream/commit/e33dab4e3963ce01c28e7095918757a7b4beffe5))


## v0.5.0 (2026-11-10)

### Features

- Add the firewall helper and close the stream port when the daemon starts
  ([#7](https://github.com/skj-skj/vpstream/pull/7),
  [`1584aa2`](https://github.com/skj-skj/vpstream/commit/1584aa2b4581cd915b03514ffc52a172bc2ca487))


## v0.4.0 (2026-11-10)

### Features

- Add the daemon with its unix-socket CLI and Tailscale-only dashboard port
  ([#6](https://github.com/skj-skj/vpstream/pull/6),
  [`acdef5e`](https://github.com/skj-skj/vpstream/commit/acdef5ec4f27005cf00c419e22040d34b297c2a8))


## v0.3.0 (2026-11-10)

### Features

- Add vpstream doctor and the configuration file ([#5](https://github.com/skj-skj/vpstream/pull/5),
  [`364af62`](https://github.com/skj-skj/vpstream/commit/364af6266dbaee33c960a6cc0a856c28e33d40a8))


## v0.2.1 (2026-11-10)

### Bug Fixes

- Install fallback fonts for burned-in subtitles ([#4](https://github.com/skj-skj/vpstream/pull/4),
  [`edc367d`](https://github.com/skj-skj/vpstream/commit/edc367d79ac70e990145162921282a691d33cabd))


## v0.2.0 (2026-11-09)

### Features

- Add vpstream install ffmpeg and vpstream update ffmpeg
  ([#3](https://github.com/skj-skj/vpstream/pull/3),
  [`ae667aa`](https://github.com/skj-skj/vpstream/commit/ae667aa105a9f9cd1248d851003893903a990ea6))


## v0.1.1 (2026-11-09)

### Bug Fixes

- Read mkvmerge WebVTT subtitles and keep image subtitles after a seek
  ([#2](https://github.com/skj-skj/vpstream/pull/2),
  [`d664e7f`](https://github.com/skj-skj/vpstream/commit/d664e7fa3170558cd22af4ad74a85e02f866bdc6))


## v0.1.0 (2026-11-09)

- Initial Release
