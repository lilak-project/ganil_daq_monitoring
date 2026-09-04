#!/usr/bin/env bash
# Start the GANIL DAQ monitoring service in the background.
#
#   ./start_server.sh                 port 8008
#   ./start_server.sh --port 8009
#
# Read-only: it follows the run-control logs the DAQ writes under /data1 and
# has no path into the acquisition at all. Starting or stopping it cannot
# affect a run in progress.
#
# Any option is passed straight through to backend/run.py.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pidfile="$here/.ganil_daq_monitoring.pid"
logfile="$here/ganil_daq_monitoring.server.log"

if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  echo "Already running as PID $(cat "$pidfile"). Stop it with ./stop_server.sh first." >&2
  exit 1
fi

if [[ ! -d "$here/frontend/dist" ]]; then
  echo "The UI is not built. Run: cd frontend && npm install && npm run build" >&2
  exit 1
fi

cd "$here/backend"
nohup python3 run.py "$@" >>"$logfile" 2>&1 &
pid=$!
echo "$pid" >"$pidfile"

# Everything from here is reporting, and none of it may take the server down
# with it. Piping this script into `head` closes its stdout early, and under
# `set -o pipefail` the SIGPIPE that follows would abort the script after the
# server had already been started -- leaving it running with nobody holding
# the pid. So the rest is best-effort.
set +e

sleep 2
tail -n 20 "$logfile" 2>/dev/null
echo
echo "PID $pid · log: $logfile"

# Worth checking rather than assuming: a port already in use, or a syntax
# error, exits within the two seconds above and would otherwise be reported
# here as a successful start.
if ! kill -0 "$pid" 2>/dev/null; then
  echo
  echo "It exited immediately. The reason is at the end of $logfile." >&2
  rm -f "$pidfile"
  exit 1
fi
exit 0
