import os
from pathlib import Path

from celery import Celery
from kombu.exceptions import OperationalError

queue = Celery(
    "seminar", broker="redis://broker:6379/0", backend="redis://broker:6379/1"
)
queue.conf.update(
    task_default_queue="seminar",
    task_track_started=True,
    result_expires=3600,
    worker_prefetch_multiplier=1,
)

@queue.task
def word_count(text):
    return len(text.split())


def memory_ratio():
    root = Path("/sys/fs/cgroup")
    return int((root / "memory.current").read_text()) / int((root / "memory.max").read_text())


if __name__ == "__main__":
    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.get("/health")
    def health():
        try:
            replies = queue.control.ping(destination=["seminar@worker"], timeout=0.6)
            ratio = memory_ratio()
        except (ValueError, OSError, OperationalError):
            return jsonify(ok=False), 503
        return jsonify(ok=bool(replies), memory_ratio=ratio), 200 if replies else 503

    app.run(host="0.0.0.0", port=int(os.environ.get("PROBE_PORT", "8001")))
