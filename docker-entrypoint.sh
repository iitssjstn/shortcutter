#!/bin/sh
# Start als root alleen om de rechten van de volumes goed te zetten, en draai de app
# daarna als de niet-root gebruiker 'app' (uid 1000).
set -e
if [ "$(id -u)" = "0" ]; then
  for d in /data /cache /cache/huggingface; do
    mkdir -p "$d"
    if [ "$(stat -c %u "$d")" != "1000" ]; then
      chown -R app:app "$d"
    fi
  done
  exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
