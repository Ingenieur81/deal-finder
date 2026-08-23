#!/bin/sh
set -eu

# The bind-mounted SQLite directory can be created on the host as root or a
# different UID. Correct it before dropping privileges for the application.
mkdir -p /data
chown -R appuser:appuser /data

# `sh -c` consumes its first argument as $0. Keep a placeholder there so
# `$@` still starts with the executable from Docker's CMD (uvicorn).
exec su -s /bin/sh -c 'exec "$@"' -- appuser entrypoint "$@"
