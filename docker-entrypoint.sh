#!/bin/sh
set -eu

data_dir=$(dirname "${JOBS_PATH:-/app/data/jobs.json}")

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data_dir"
    chown -R app:app "$data_dir"
    exec gosu app "$@"
fi

exec "$@"
