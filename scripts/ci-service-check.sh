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

step "the helper opens and closes only the configured port"
sudo -u vpstream sudo -n "$FW" open
has_rule "ufw allow 1337/tcp"
sudo -u vpstream sudo -n "$FW" close
no has_rule 1337
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
