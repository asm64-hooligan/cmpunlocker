#!/bin/bash
# Build fbpa_dump in a root-only directory and run it (read-only):  sudo bash tools/hbmtune/fbpa_dump.sh [report-file]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd)/fbpa_dump.c
OUT=${1:-}
T=$(mktemp -d /root/fbpa-dump.XXXXXX); trap 'rm -rf "$T"' EXIT
cp "$SRC" "$T/fbpa_dump.c"; cc -O2 -Wall -o "$T/fbpa_dump" "$T/fbpa_dump.c"
{ echo "fbpa_dump $(date -u +%Y-%m-%dT%H:%M:%SZ)  driver: $(cat /sys/module/nvidia/version 2>/dev/null)"; nvidia-smi --query-gpu=index,pci.bus_id,serial,clocks.mem --format=csv,noheader 2>/dev/null; echo; "$T/fbpa_dump"; } | if [ -n "$OUT" ]; then tee "$OUT"; chmod 644 "$OUT"; else cat; fi
