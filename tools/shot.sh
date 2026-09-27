#!/bin/sh
# dev: send navigation commands to the GUI and fetch a screenshot
# usage: tools/shot.sh OUT.png [command ...]
HOST=${WYSE:-ingest.local}
OUT=$1; shift
CMDS=$(printf '%s\n' "$@")
.venv/Scripts/python tools/wyse.py sh "$HOST" "printf '%s' '$CMDS' > /tmp/ingest-gui.cmd; P=\$(pgrep -f 'python3 /usr/local/sbin/ingest-gui' | head -1); [ -n \"$*\" ] && kill -USR2 \$P; sleep 1; rm -f /tmp/ingest-gui.png; kill -USR1 \$P; sleep 2; ls /tmp/ingest-gui.png >/dev/null" >/dev/null && .venv/Scripts/python -c "
import sys; sys.path.insert(0,'tools'); import wyse
c = wyse.connect('$HOST'); s = c.open_sftp(); s.get('/tmp/ingest-gui.png', '$OUT'); print('$OUT')"
