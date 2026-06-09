#!/usr/bin/with-contenv sh

set -eu

QBITTORRENT_DIR=/config/qBittorrent

if pgrep -x qbittorrent-nox >/dev/null 2>&1; then
  echo "qBittorrent is already running; leaving runtime lock files untouched."
  exit 0
fi

rm -f "$QBITTORRENT_DIR/lockfile" "$QBITTORRENT_DIR/ipc-socket"
