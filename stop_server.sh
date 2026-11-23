#!/usr/bin/env bash
# Stop the GANIL DAQ control service.
#
# Stopping it stops the watch, and a run boundary that happens while it is down
# is not pushed to elog -- but it is not LOST either: the boundary is in
# rcc_gui.txt, and on the next start the service adopts the log as it stands.
# What it will not do is push the runs it missed, on purpose.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pidfile="$here/.ganil_daq_monitoring.pid"

if [[ ! -f "$pidfile" ]]; then
  echo "Not running (no $pidfile)."
  exit 0
fi

pid="$(cat "$pidfile")"
if ! kill -0 "$pid" 2>/dev/null; then
  echo "Not running (PID $pid is gone); clearing the pid file."
  rm -f "$pidfile"
  exit 0
fi

kill "$pid"
for _ in $(seq 20); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.25
done
if kill -0 "$pid" 2>/dev/null; then
  echo "PID $pid did not stop; sending SIGKILL." >&2
  kill -9 "$pid" 2>/dev/null || true
fi
rm -f "$pidfile"
echo "Stopped."
