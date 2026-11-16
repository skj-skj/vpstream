#!/usr/bin/env bash
# TEMPORARY (milestone 7 validation, removed before the PR): what a whole install, use and
# removal leaves on a fresh runner. Prints everything; asserts nothing on purpose.
set -uo pipefail
[ "${CI:-}" = "true" ] || { echo "CI only" >&2; exit 2; }
step() { echo; echo "=================== $*"; }
ROOTS=(/etc /var/lib /var/cache /var/log /opt /srv /usr/local /run /home /root /tmp)
snap() {
  sudo find "${ROOTS[@]}" -xdev \( -path /run/user -o -path /proc -o -path /var/lib/docker \
    -o -path /var/lib/containerd -o -path /tmp/snap-\* -o -path /var/log/journal \) -prune \
    -o -printf '%p %y %m %u:%g\n' 2>/dev/null | sort > "$1"
}

step "runner baseline"
. /etc/os-release; echo "$PRETTY_NAME"
command -v ffmpeg ffprobe caddy pipx ufw setfacl || true
dpkg-query -W -f='${Package} ${Status}\n' ffmpeg libgcc-s1 fontconfig fonts-dejavu-core \
  fonts-liberation fonts-noto-cjk 2>&1 || true
snap list 2>/dev/null | grep -i ffmpeg || echo "no ffmpeg snap"
ls -la /opt /srv /usr/local/bin | head -50
getfacl -c /opt /srv 2>/dev/null || true

step "prerequisites (as a user would: apt), then the baseline snapshot"
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends ufw pipx caddy acl mkvtoolnix \
  libgcc-s1 fontconfig fonts-dejavu-core fonts-liberation fonts-noto-cjk > /dev/null
sudo systemctl disable --now caddy
sudo install -m 0755 scripts/ci-fake-tailscale /usr/local/bin/tailscale
snap /tmp/snap-0.txt
wc -l /tmp/snap-0.txt

step "pipx install for the whole system"
wheel="$(ls "$PWD"/dist/*.whl)"
sudo PIPX_HOME=/srv/vpstream-pipx PIPX_BIN_DIR=/usr/local/bin \
  /usr/bin/pipx install --python /usr/bin/python3 "${wheel}[dev]"
vpstream --version
ls -la /usr/local/bin
snap /tmp/snap-1.txt; diff /tmp/snap-0.txt /tmp/snap-1.txt | grep -v '/srv/vpstream-pipx/' | head -40
echo "pipx files: $(grep -c '/srv/vpstream-pipx' /tmp/snap-1.txt)"

step "doctor before init (exit code printed)"
vpstream doctor; echo "doctor exit $?"

step "init, doctor, selftest"
sudo vpstream init; echo "init exit $?"
sg vpstream -c "vpstream doctor"; echo "doctor exit $?"
sg vpstream -c "vpstream selftest -q" 2>&1 | tail -3

step "a stream"
sudo mkdir -p /srv/m7 && head -c 300000 /dev/urandom | sudo tee /srv/m7/film.mkv > /dev/null
sudo chmod 755 /srv/m7 && sudo chmod 644 /srv/m7/film.mkv
sg vpstream -c "vpstream start --file /srv/m7/film.mkv --alias ci"
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:1337/ci
sg vpstream -c "vpstream stop"
sudo rm -r /srv/m7

step "vpstream install ffmpeg (the real build)"
vpstream install ffmpeg --yes; echo "install exit $?"
ls -la /usr/local/bin /opt/vpstream-ffmpeg /opt/vpstream-ffmpeg/bin
cat /opt/vpstream-ffmpeg/vpstream-build.json
command -v ffmpeg; readlink -f "$(command -v ffmpeg)"
snap /tmp/snap-2.txt; diff /tmp/snap-1.txt /tmp/snap-2.txt | head -80

step "uninstall --purge"
sudo vpstream uninstall --purge --yes; echo "uninstall exit $?"
snap /tmp/snap-3.txt; diff /tmp/snap-1.txt /tmp/snap-3.txt | head -80

step "by hand: what the new uninstall would remove (ffmpeg)"
for p in ffmpeg ffprobe; do
  l=/usr/local/bin/$p
  [ -L "$l" ] && case "$(readlink -f "$l")" in /opt/vpstream-ffmpeg/*) sudo rm "$l";; esac
done
sudo rm -rf /opt/vpstream-ffmpeg

step "pipx uninstall"
sudo PIPX_HOME=/srv/vpstream-pipx PIPX_BIN_DIR=/usr/local/bin /usr/bin/pipx uninstall vpstream
echo "pipx uninstall exit $?"
sudo find /srv/vpstream-pipx 2>/dev/null | head -20
snap /tmp/snap-4.txt
echo "--- final diff against the baseline (everything left behind or gone)"
diff /tmp/snap-0.txt /tmp/snap-4.txt
echo "--- end"
getent passwd vpstream || echo "no user"; getent group vpstream || echo "no group"
sudo ufw show added
