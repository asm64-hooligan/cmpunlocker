#!/bin/bash
# Install hbmtune as root-owned copies (run from the checkout):  sudo tools/hbmtune/install-hbmtune.sh [site.conf]
# Root never runs anything from a user-writable directory afterwards: the tool, the gates and the unit are copied to
# /usr/local/lib/hbmtune and /etc. An optional site config is copied to /etc/hbmtune/hbmtune.conf when none exists.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)
install -d -o root -g root -m 0755 /usr/local/lib/hbmtune /etc/hbmtune /var/lib/hbmtune
for f in hbmtune.py gates.py; do install -o root -g root -m 0755 "$SRC/$f" /usr/local/lib/hbmtune/$f; done
python3 /usr/local/lib/hbmtune/hbmtune.py --help >/dev/null
ln -sf /usr/local/lib/hbmtune/hbmtune.py /usr/local/sbin/hbmtune
if [ ! -f /etc/hbmtune/hbmtune.conf ]; then
  if [ -n "${1:-}" ]; then install -o root -g root -m 0644 "$1" /etc/hbmtune/hbmtune.conf
  else printf '# hbmtune site settings; see tools/hbmtune/README.md\nbaseline_ndiv = 0\napply_mode = reboot\n' > /etc/hbmtune/hbmtune.conf; fi
  echo "wrote /etc/hbmtune/hbmtune.conf"
else echo "kept the existing /etc/hbmtune/hbmtune.conf"; fi
install -o root -g root -m 0644 "$SRC/hbmtune-resume.service" /etc/systemd/system/hbmtune-resume.service
systemctl daemon-reload; systemctl enable hbmtune-resume.service
echo "installed: hbmtune (sha256 $(sha256sum /usr/local/lib/hbmtune/hbmtune.py | cut -c1-16)), gates (sha256 $(sha256sum /usr/local/lib/hbmtune/gates.py | cut -c1-16))"
echo "next: sudo hbmtune status"
