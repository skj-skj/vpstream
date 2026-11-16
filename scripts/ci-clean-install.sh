#!/usr/bin/env bash
# A fresh host from nothing to a stream and back to nothing, as the README says: pipx install,
# doctor, install caddy and ffmpeg, selftest, then init, streams and uninstall (by
# ci-service-check.sh), uninstall --purge and pipx uninstall. At the end the file system holds
# nothing of vpstream's: what is left must be pipx's, pip's, the system's, or what
# `vpstream install caddy` added (Caddy stays, see Uninstall in the README).
#
#   ci-clean-install.sh WHEEL                      the whole check (CI only)
#   ci-clean-install.sh leftovers BEFORE AFTER KEPT  what differs and is not allowed (reads
#                                                    files only; runs anywhere)
set -euo pipefail

PIPX_HOME=/srv/vpstream-pipx  # not /opt: the runner's /opt is writable by every user
# `env`, not `sudo VAR=...`: on the 24.04 image pam_env sets PIPX_HOME from /etc/environment
# over the command line's.
PIPX=(sudo env "PIPX_HOME=$PIPX_HOME" PIPX_BIN_DIR=/usr/local/bin /usr/bin/pipx)
ROOTS=(/etc /var/lib /var/cache /var/log /var/tmp /opt /srv /usr/local /usr/share/keyrings
  /run /home /root)
# Left by others, never vpstream: pipx's own home (its shared pip, logs and lock; the venv
# itself must go), root's pip cache, a user's standard ~/.local/state folders (the folders
# only), the system's ufw lock, man cache, logrotate state, sudo's timestamps and systemd's
# transient units and private temporary folders, the runner's Azure agent, and the fake
# tailscale's own state.
ALLOWED="^[<>] ($PIPX_HOME[/ ]|/root/\.cache[/ ]|/home/[^/ ]+/\.local(/state)? d |\
/run/ufw\.lock |/var/cache/man[/ ]|/var/lib/logrotate/status |/run/sudo[/ ]|\
/run/systemd/transient[/ ]|/var/tmp/systemd-private-|/var/lib/waagent[/ ]|\
/var/tmp/ci-fake-tailscale\.json )"
OURS="^[<>] ($PIPX_HOME/venvs/vpstream[/ ]|/usr/local/bin/vpstream|/opt/vpstream-ffmpeg|\
/home/[^/ ]+/\.local/state/vpstream[/ ])"

snapshot() {  # every entry's path, type, mode and owner
  # An entry that goes away during the walk (under /run, say) makes find exit 1: that is
  # fine, any other complaint is not.
  { sudo find "${ROOTS[@]}" -xdev \( -path /run/user -o -path /var/log/journal \
    -o -path /var/lib/docker -o -path /var/lib/containerd \) -prune \
    -o -printf '%p %y %m %u:%g\n' 2> "$1.err" || true; } | LC_ALL=C sort > "$1"
  [ -s "$1" ]
  if grep -v 'No such file or directory' "$1.err"; then
    echo "!! find could not see everything" >&2; exit 1
  fi
}

leftovers() {  # BEFORE AFTER KEPT: changed entries, less KEPT's lines and the allowed ones
  local changed
  changed=$(diff "$1" "$2" | grep '^[<>] ' || true)
  {
    { grep -vxF -f "$3" <<< "$changed" | grep -vE "$ALLOWED" || true; } | grep . || true
    grep -E "$OURS" <<< "$changed" || true
  } | sort -u
}

if [ "${1:-}" = leftovers ]; then
  leftovers "$2" "$3" "$4"
  exit 0
fi
. "$(dirname "$0")/ci-lib.sh"
WHEEL=$1
[ -f "$WHEEL" ]
W=$(mktemp -d)

step "the host before vpstream"
no command -v vpstream
no command -v ffmpeg
snapshot "$W/before.txt"
getent passwd > "$W/passwd.txt"; getent group > "$W/group.txt"
sudo ufw show added > "$W/ufw.txt"

step "pipx install for the whole system"
"${PIPX[@]}" install --python /usr/bin/python3 "${WHEEL}[dev]"
hash -r
[ "$(command -v vpstream)" = /usr/local/bin/vpstream ]
vpstream --version | grep -E '^vpstream [0-9]+\.[0-9]+\.[0-9]+$'

step "doctor before init: what is missing, each with its fix"
no vpstream doctor > "$W/doctor-0.txt"
cat "$W/doctor-0.txt"
grep -qE '^PROBLEM +ffmpeg ' "$W/doctor-0.txt"
grep -qF 'fix: vpstream install ffmpeg' "$W/doctor-0.txt"
grep -qE "^ok +install +$PIPX_HOME/" "$W/doctor-0.txt"
grep -qE '^warning +service +not set up' "$W/doctor-0.txt"

step "vpstream install caddy: what it adds stays after the uninstall"
snapshot "$W/caddy-0.txt"
vpstream install caddy --yes
snapshot "$W/caddy-1.txt"
diff "$W/caddy-0.txt" "$W/caddy-1.txt" | grep '^[<>] ' > "$W/kept.txt" || true
wc -l < "$W/kept.txt"
no grep -E "$OURS" "$W/kept.txt"

step "vpstream install ffmpeg: the real build (GITHUB_TOKEN for GitHub's API limit)"
vpstream install ffmpeg --yes
hash -r
[ "$(readlink -f "$(command -v ffmpeg)")" = /opt/vpstream-ffmpeg/bin/ffmpeg ]
[ "$(readlink -f "$(command -v ffprobe)")" = /opt/vpstream-ffmpeg/bin/ffprobe ]
vpstream doctor > "$W/doctor-1.txt" || true
grep -qE '^ok +ffmpeg ' "$W/doctor-1.txt"
grep -qE '^ok +ffprobe ' "$W/doctor-1.txt"

step "selftest from the system install"
(cd "$W" && vpstream selftest -- -k "not record_and_its_folder")  # MUTATION run only

step "init, streams, the helper, crashes and uninstall"
"$(dirname "$0")/ci-service-check.sh"

step "uninstall --purge takes the ffmpeg vpstream installed"
sudo vpstream uninstall --purge --yes | tee "$W/purge.txt"
grep -qxF "/usr/local/bin/ffmpeg: removed" "$W/purge.txt"
grep -qxF "/usr/local/bin/ffprobe: removed" "$W/purge.txt"
no sudo test -e /opt/vpstream-ffmpeg
no sudo test -L /usr/local/bin/ffmpeg
no sudo test -L /usr/local/bin/ffprobe

step "pipx uninstall"
"${PIPX[@]}" uninstall vpstream
hash -r
no command -v vpstream
no sudo test -e "$PIPX_HOME/venvs/vpstream"

step "nothing of vpstream's is left"
snapshot "$W/after.txt"
leftovers "$W/before.txt" "$W/after.txt" "$W/kept.txt" | tee "$W/left.txt"
[ ! -s "$W/left.txt" ]
getent passwd | diff "$W/passwd.txt" -
getent group | diff "$W/group.txt" -
sudo ufw show added | diff "$W/ufw.txt" -
echo; echo "the clean install check passed"
