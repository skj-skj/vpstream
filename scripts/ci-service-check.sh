#!/usr/bin/env bash
# Runs the real `vpstream init` and `vpstream uninstall` on a throwaway CI machine and checks
# that they do what they say and leave no trace. It changes users, sudoers, systemd and ufw
# rules, so it refuses to run anywhere but CI. The machine is on no tailnet, so `tailscale` is
# scripts/ci-fake-tailscale there; the real `tailscale serve` is checked on a real host.
set -euo pipefail
[ "${CI:-}" = "true" ] || { echo "this script is for CI machines only" >&2; exit 2; }

BIN=$(dirname "$(readlink -f "$(command -v vpstream)")")
FW="$BIN/vpstream-fw"
rules() { sudo ufw show added; }
# Through a file, so a grep that stops reading early cannot fail the pipe.
has_rule() { rules > /tmp/ufw-now.txt; grep -q -- "$1" /tmp/ufw-now.txt; }
as_group() { sg vpstream -c "$*"; }  # this login got the group after it started
step() { echo; echo "== $*"; }
no() { if "$@"; then echo "!! expected to fail: $*" >&2; exit 1; fi; }

step "before"
rules > /tmp/ufw-before.txt
tailscale serve status --json > /tmp/serve-before.json
grep -q '"443"' /tmp/serve-before.json
getent passwd > /tmp/passwd-before.txt
getent group > /tmp/group-before.txt
sudo ls /etc/sudoers.d > /tmp/sudoers-before.txt
no getent passwd vpstream

step "a normal user is told to use sudo"
no vpstream init

step "init"
sudo vpstream init
systemctl is-active vpstream
systemctl is-enabled vpstream
sudo visudo -c
id -nG "$USER" | tr ' ' '\n' | grep -qx vpstream
rules | diff /tmp/ufw-before.txt -   # init adds no ufw rule

step "the dashboard listens on loopback only and is published by tailscale serve"
ss -ltn | grep -w 1338 | tee /tmp/listen.txt
[ "$(wc -l < /tmp/listen.txt)" = 1 ] && grep -q "127.0.0.1:1338" /tmp/listen.txt
curl -sS -m 5 http://127.0.0.1:1338/api/v1/status | grep -q '"version"'
tailscale serve status --json | tee /tmp/serve-now.json
grep -q '"Proxy": "http://127.0.0.1:1338"' /tmp/serve-now.json
grep -q '"Proxy": "http://127.0.0.1:2283"' /tmp/serve-now.json

step "the CLI talks to the daemon over the socket (group only)"
sudo ls -la /run/vpstream
as_group "$BIN/vpstream status"
as_group "$BIN/vpstream status --json" > /tmp/status.json
grep -q '"stream_port_state": "closed"' /tmp/status.json
grep -q '"firewall_problem": null' /tmp/status.json
grep -q '"url": "https://ci-host.example.ts.net:1338/"' /tmp/status.json
no sudo -u nobody "$BIN/vpstream" status
vpstream doctor || true

step "a stream: the real Caddy serves the file, and the port is open only while it runs"
MEDIA=/srv/vpstream-ci-media
status_code() { curl -s -m 10 -o /dev/null -w '%{http_code}' "$1"; }
sudo mkdir -p "$MEDIA"
# Someone else's private file: start cannot open it up, and says who can.
head -c 1000 /dev/urandom | sudo tee "$MEDIA/roots.mkv" > /dev/null
sudo chmod 600 "$MEDIA/roots.mkv"
no as_group "$BIN/vpstream start --file $MEDIA/roots.mkv" 2> /tmp/refused.txt
cat /tmp/refused.txt
grep -q "sudo setfacl -m u:vpstream:rX $MEDIA/roots.mkv" /tmp/refused.txt
no has_rule 1337
# The caller's own private library: no chmod and no group change, start gives the service
# access by itself, to its user alone.
sudo chown "$USER" "$MEDIA"
chmod 700 "$MEDIA"
head -c 300000 /dev/urandom > "$MEDIA/film.mkv"
chmod 600 "$MEDIA/film.mkv"
no sudo -u vpstream cat "$MEDIA/film.mkv" > /dev/null
as_group "$BIN/vpstream start --file $MEDIA/film.mkv --alias ci" 2> /tmp/granted.txt
cat /tmp/granted.txt
grep -q "Gave the vpstream service (user vpstream) read access to 2 of your" /tmp/granted.txt
getfacl -c "$MEDIA/film.mkv" | tee /tmp/acl.txt
grep -qx "user:vpstream:r--" /tmp/acl.txt
grep -qx "other::---" /tmp/acl.txt
no sudo -u nobody cat "$MEDIA/film.mkv" > /dev/null   # still closed to everyone else
has_rule "ufw allow 1337/tcp"
[ "$(ps -o user= -C caddy | sort -u | tr -d ' ')" = vpstream ]
curl -sS -m 10 -D /tmp/film.headers -o /tmp/film.out http://127.0.0.1:1337/ci
sudo cmp /tmp/film.out "$MEDIA/film.mkv"
grep -qi '^content-type: video/x-matroska' /tmp/film.headers
[ "$(curl -sS -m 10 -r 10-19 http://127.0.0.1:1337/ci/1 | wc -c)" = 10 ]
[ "$(status_code http://127.0.0.1:1337/)" = 404 ]
[ "$(status_code http://127.0.0.1:1337/ci/2)" = 404 ]
no curl -s -m 3 -o /dev/null http://127.0.0.1:2019/config/   # Caddy's admin port stays shut
as_group "$BIN/vpstream devices lock"
as_group "$BIN/vpstream devices disable 127.0.0.1"
[ "$(status_code http://127.0.0.1:1337/ci)" = 403 ]
as_group "$BIN/vpstream devices" | tee /tmp/devices.txt
grep -q "127.0.0.1  disabled" /tmp/devices.txt
as_group "$BIN/vpstream status"

step "a crash: the stream, its port and its devices come back"
sudo systemctl kill -s KILL vpstream
sleep 10
systemctl is-active vpstream
has_rule "ufw allow 1337/tcp"
[ "$(status_code http://127.0.0.1:1337/ci)" = 403 ]
as_group "$BIN/vpstream devices enable 127.0.0.1"
[ "$(status_code http://127.0.0.1:1337/ci)" = 200 ]

step "stop closes the port, ends Caddy and takes the file access back"
as_group "$BIN/vpstream stop" | tee /tmp/stopped.txt
grep -q "Took back the vpstream service's access to 2 of your" /tmp/stopped.txt
no grep -q vpstream <(getfacl -c "$MEDIA/film.mkv" "$MEDIA")
no sudo -u vpstream cat "$MEDIA/film.mkv" > /dev/null
no has_rule 1337
no pgrep -x caddy
no curl -s -m 3 -o /dev/null http://127.0.0.1:1337/ci

step "a restart of the service ends the stream"
as_group "$BIN/vpstream start --file $MEDIA/film.mkv --alias ci"
has_rule "ufw allow 1337/tcp"
sudo systemctl restart vpstream
sleep 3
no has_rule 1337
no pgrep -x caddy
as_group "$BIN/vpstream status --json" | grep -q '"stream": null'
# That stream ended without `vpstream stop`, so its access is still there: revoke takes it.
getfacl -c "$MEDIA/film.mkv" | grep -qx "user:vpstream:r--"
as_group "$BIN/vpstream revoke"
no grep -q vpstream <(getfacl -c "$MEDIA/film.mkv" "$MEDIA")
sudo rm -r "$MEDIA"

step "the helper opens and closes only the configured port"
sudo -u vpstream sudo -n "$FW" open
has_rule "ufw allow 1337/tcp"
sudo grep -qx 1337 /etc/vpstream/open-ports   # written down, so it can always be closed
sudo -u vpstream sudo -n "$FW" close
no has_rule 1337
no sudo test -e /etc/vpstream/open-ports
no sudo -u vpstream sudo -n "$FW" open 22
no sudo -u vpstream sudo -n "$FW" status
no sudo -u vpstream sudo -n /usr/sbin/ufw status
no "$FW" open

step "a restart closes the stream port"
sudo -u vpstream sudo -n "$FW" open
sudo systemctl restart vpstream
sleep 3
no has_rule 1337

step "a crash closes the stream port, and the service comes back"
sudo -u vpstream sudo -n "$FW" open
sudo systemctl kill -s KILL vpstream
sleep 10
no has_rule 1337
systemctl is-active vpstream

step "init again changes nothing"
sudo cat /etc/vpstream/install.json > /tmp/manifest-1.json
sudo vpstream init
sudo cat /etc/vpstream/install.json | diff /tmp/manifest-1.json -
systemctl is-active vpstream

step "uninstall leaves no trace"
sudo vpstream uninstall --yes
no getent passwd vpstream
no getent group vpstream
for path in /etc/vpstream /var/lib/vpstream /var/cache/vpstream /run/vpstream \
    /etc/systemd/system/vpstream.service /etc/sudoers.d/vpstream; do
  no sudo test -e "$path"
done
rules | diff /tmp/ufw-before.txt -
tailscale serve status --json | diff /tmp/serve-before.json -
getent passwd | diff /tmp/passwd-before.txt -
getent group | diff /tmp/group-before.txt -
sudo ls /etc/sudoers.d | diff /tmp/sudoers-before.txt -
sudo visudo -c
no systemctl is-active vpstream
no systemctl is-enabled vpstream

step "uninstall again is harmless"
sudo vpstream uninstall --yes
echo; echo "all service checks passed"
