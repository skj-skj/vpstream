#!/usr/bin/env bash
# Runs the real `vpstream init` and `vpstream uninstall` on a throwaway CI machine and checks
# that they do what they say and leave no trace. It changes users, sudoers, systemd and ufw
# rules, so it refuses to run anywhere but CI. The machine is on no tailnet, so `tailscale` is
# scripts/ci-fake-tailscale there; the real `tailscale serve` is checked on a real host.
set -euo pipefail
. "$(dirname "$0")/ci-lib.sh"

BIN=$(dirname "$(readlink -f "$(command -v vpstream)")")
FW="$BIN/vpstream-fw"
rules() { sudo ufw show added; }
# Through a file, so a grep that stops reading early cannot fail the pipe.
has_rule() { rules > /tmp/ufw-now.txt; grep -q -- "$1" /tmp/ufw-now.txt; }
as_group() { sg vpstream -c "$*"; }  # this login got the group after it started

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
grep -qF "Its owner can: setfacl -m u:vpstream:rX $MEDIA/roots.mkv (root: with sudo)" /tmp/refused.txt
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
no grep -q '^mask::' <(getfacl -c "$MEDIA/film.mkv" "$MEDIA")   # no ACL left at all
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

step "watch together: two pages, a press of one, and the other's held wait answered on it"
# The tests' own sample (the wheel carries them), prepared for browsers by the service.
"$BIN/python" -c 'import sys; from pathlib import Path; from vpstream.tests.media import make_sample
make_sample(Path(sys.argv[1]))' "$MEDIA"
chmod 600 "$MEDIA/sample.mkv"
as_group "$BIN/vpstream start --file $MEDIA/sample.mkv --alias tg --mode browser --together --yes"
json() { "$BIN/python" -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
PLAYER='X-Vpstream-Player: 1'
page() { curl -sS -m 10 -b "/tmp/$1.jar" -c "/tmp/$1.jar" -H 'Accept: text/html' -o "/tmp/$1.html" -w '%{http_code}' http://127.0.0.1:1337/tg; }
report() { curl -sS -m 10 -b "/tmp/$1.jar" -H "$PLAYER" "http://127.0.0.1:1337/tg/1/client?pg=$2&t=0&d=12&p=1&in=1${3:-}"; }
for who in tv phone; do
  [ "$(page $who)" = 403 ]   # it waits for the owner
  id=$(as_group "$BIN/vpstream status --json" | json 'next(x["id"] for x in d["stream"]["devices"] if x["allowed"] is None)')
  as_group "$BIN/vpstream devices enable $id"
  [ "$(page $who)" = 200 ]
  grep -q '"sync": "/tg/1/sync"' "/tmp/$who.html"
done
[ "$(report tv tv000001 | json 'd["cooldown"]')" = 10.0 ]   # just joined: its buttons wait
report phone ph000001 > /dev/null
sleep 10
rev=$(report tv tv000001 | json 'd["together"]["rev"]')
curl -sS -m 30 -b /tmp/tv.jar -H "$PLAYER" -w '\n%{time_total}\n' \
  "http://127.0.0.1:1337/tg/1/sync?pg=tv000001&rev=$rev" > /tmp/held.txt &
held=$!
sleep 2   # the wait is held by now
[ "$(report phone ph000001 '&act=play&id=ph000001-1' | json 'd["outcome"]')" = applied ]
wait "$held"
cat /tmp/held.txt
head -1 /tmp/held.txt | json 'd["together"]["paused"]' | grep -qx False
[ "$(head -1 /tmp/held.txt | json 'd["together"]["rev"]')" = $((rev + 1)) ]
# Answered on the change, a moment after the press: not when its hold ran out.
"$BIN/python" -c "import sys; assert float(sys.argv[1]) < 6" "$(tail -1 /tmp/held.txt)"
as_group "$BIN/vpstream together pause"   # inside the press's window: settled when it ends
sleep 3
as_group "$BIN/vpstream status --json" | json '"%s %s" % (d["stream"]["together"]["paused"], len(d["stream"]["together"]["pages"]))' | grep -qx 'True 2'
as_group "$BIN/vpstream stop"
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

step "HTTPS: the current Caddy, by vpstream install caddy, over Ubuntu's 2.6.2"
vpstream install caddy --yes
caddy version
no systemctl is-active --quiet caddy   # the package's own web server stays off (port 80)
no systemctl is-enabled --quiet caddy

step "init --https: the service may bind port 80, and still reaches root through sudo"
# An authority where nothing listens: Let's Encrypt is never asked, each request fails at once.
printf '\n[acme]\nca = "https://127.0.0.1:9/directory"\n' | sudo tee -a /etc/vpstream/config.toml
sudo vpstream init --https
systemctl is-active vpstream
UNIT=/etc/systemd/system/vpstream.service
grep -qx "AmbientCapabilities=CAP_NET_BIND_SERVICE" "$UNIT"
no grep -q CapabilityBoundingSet "$UNIT"   # it would keep sudo from becoming root
grep CapAmb "/proc/$(systemctl show -p MainPID --value vpstream)/status" | tee /tmp/capamb.txt
grep -q "0000000000000400" /tmp/capamb.txt   # CAP_NET_BIND_SERVICE alone
MEDIA=/srv/vpstream-ci-media
sudo mkdir -p "$MEDIA"
head -c 300000 /dev/urandom | sudo tee "$MEDIA/film.mkv" > /dev/null
sudo chmod 755 "$MEDIA" && sudo chmod 644 "$MEDIA/film.mkv"
no as_group "$BIN/vpstream start --file $MEDIA/film.mkv --alias ci --ttl 60" 2> /tmp/https.txt
cat /tmp/https.txt
grep -q "no HTTPS certificate for the stream" /tmp/https.txt
grep -q "vpstream start --http" /tmp/https.txt
no grep -q "could not open" /tmp/https.txt     # sudo and the helper work from this unit
no grep -q "may not listen" /tmp/https.txt     # and the service may bind port 80
no sudo grep -q -- "--dport 80 " /etc/ufw/user.rules /etc/ufw/user6.rules  # closed again
no sudo test -e /etc/vpstream/open-acme-ports
no pgrep -f "caddy/certificate/caddy.json"     # the requesting Caddy is gone
as_group "$BIN/vpstream start --file $MEDIA/film.mkv --alias ci --http"
has_rule "ufw allow 1337/tcp"
curl -sS -m 10 -o /tmp/film.out http://127.0.0.1:1337/ci
cmp /tmp/film.out "$MEDIA/film.mkv"
as_group "$BIN/vpstream stop"
sudo rm -r "$MEDIA"

step "port 80 through the helper: IPv4 only, and only its own rule is removed"
# ufw's own rule files say it whether ufw is active or not (on the runner it is not).
v4_80() { sudo grep -q -- "--dport 80 " /etc/ufw/user.rules; }
v6_80() { sudo grep -q -- "--dport 80 " /etc/ufw/user6.rules; }
no v4_80
sudo -u vpstream sudo -n "$FW" acme-open
rules
v4_80
no v6_80                                     # IPv4 only: the certificate is for it
sudo grep -qx 80 /etc/vpstream/open-acme-ports
sudo -u vpstream sudo -n "$FW" acme-close
no v4_80
no v6_80
no sudo test -e /etc/vpstream/open-acme-ports
sudo ufw allow proto tcp from 0.0.0.0/0 to any port 80   # someone else's, before ours
sudo -u vpstream sudo -n "$FW" acme-open
sudo -u vpstream sudo -n "$FW" acme-close
v4_80                                        # theirs stays
sudo ufw delete allow proto tcp from 0.0.0.0/0 to any port 80
no v4_80
sudo ufw allow 80/tcp   # the usual hand-made rule, IPv4 and IPv6, before vpstream came
sudo -u vpstream sudo -n "$FW" acme-open
sudo -u vpstream sudo -n "$FW" acme-close
v4_80 && v6_80          # both halves stay
has_rule "ufw allow 80/tcp"
sudo ufw delete allow 80/tcp
no v4_80

step "init --no-https goes back"
sudo vpstream init --no-https
no grep -q AmbientCapabilities "$UNIT"
sudo grep -qx "https = false" /etc/vpstream/config.toml
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
