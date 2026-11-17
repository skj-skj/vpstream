"""What only the Linux VPS has: the systemd service, its user, sudo and the firewall
helper, ufw, tailscale serve, setfacl grants, and installs through apt."""

from vpstream.platform.common import Hints

SYSTEM_INSTALL = 'sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"'

HINTS = Hints(
    start="sudo systemctl start vpstream (the first time: sudo vpstream init)",
    stop="sudo systemctl stop vpstream",
    restart="sudo systemctl restart vpstream",
    dash_pass="sudo vpstream dash-pass",
    pip_install=SYSTEM_INSTALL,
    # The Ubuntu package that provides each program, where it differs from its name.
    packages={
        "ffprobe": "ffmpeg",
        "mkvextract": "mkvtoolnix",
        "mkvmerge": "mkvtoolnix",
        "setfacl": "acl",
    },
    package_manager="sudo apt install",
    own_installs=("ffmpeg",),
)
