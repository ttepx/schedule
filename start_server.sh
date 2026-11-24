#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ -f .schedule_server.pid ]; then
  OLD_PID="$(cat .schedule_server.pid)"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Сервер уже працює (PID $OLD_PID)"
    exit 0
  fi
fi

if [ ! -f .schedule_secret ]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > .schedule_secret
  chmod 600 .schedule_secret
fi

export SECRET_KEY="$(cat .schedule_secret)"
source .venv/bin/activate

export PORT="${PORT:-5057}"
python -c 'from app import app; app.run(debug=False, host="127.0.0.1", port=int(__import__("os").environ["PORT"]), use_reloader=False)' &
SERVER_PID=$!
echo "$SERVER_PID" > .schedule_server.pid
wait "$SERVER_PID"
