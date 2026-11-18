#!/bin/sh
set -eu
celery -A lab.tasks.queue worker --hostname=seminar@worker --concurrency=1 --loglevel=INFO &
exec python -m lab.tasks
