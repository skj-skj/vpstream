# Shared by the CI host checks (sourced, not run). They change users, sudoers, systemd and ufw
# rules, so they refuse to run anywhere but CI.
[ "${CI:-}" = "true" ] || { echo "this script is for CI machines only" >&2; exit 2; }

step() { echo; echo "== $*"; }
no() { if "$@"; then echo "!! expected to fail: $*" >&2; exit 1; fi; }
