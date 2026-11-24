#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -f .schedule_server.pid ]; then
  echo "Сервер не запущений через PID-файл."
  exit 0
fi

SERVER_PID="$(cat .schedule_server.pid)"
if kill -0 "$SERVER_PID" 2>/dev/null; then
  kill "$SERVER_PID"
  echo "Сервер зупинено (PID $SERVER_PID)"
else
  echo "Процес $SERVER_PID вже не працює."
fi
rm -f .schedule_server.pid
