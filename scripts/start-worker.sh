#!/bin/sh
set -e
python -m app.init_db
exec python -m app.worker
