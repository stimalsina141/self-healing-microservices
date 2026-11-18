import os
from time import perf_counter

import psycopg
from flask import Flask, Response, abort, g, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from redis import Redis
from redis.exceptions import RedisError

from lab.tasks import queue, word_count

app = Flask(__name__)
cache = Redis(host="cache", socket_timeout=2, socket_connect_timeout=1, decode_responses=True)
requests = Counter("lab_http_requests_total", "HTTP requests", ["route", "status"])
latency = Histogram("lab_http_request_duration_seconds", "HTTP latency", ["route"])


def database():
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2)


@app.before_request
def begin_request():
    g.started = perf_counter()


@app.after_request
def instrument(response):
    if request.endpoint not in {"metrics", "health"}:
        route = request.url_rule.rule if request.url_rule else "unmatched"
        requests.labels(route, response.status_code).inc()
        latency.labels(route).observe(perf_counter() - g.started)
    return response


@app.get("/")
def index():
    return jsonify(project="Self-Healing Microservices", routes=["/health", "/notes", "/jobs"])


@app.get("/health")
def health():
    try:
        with database() as conn:
            conn.execute("SELECT 1")
        cache.ping()
    except (psycopg.Error, RedisError, OSError):
        return jsonify(ok=False), 503
    return jsonify(ok=True)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


@app.post("/notes")
def create_note():
    body = request.get_json(silent=True) or {}
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        abort(400, "text must contain 1–4000 characters")
    with database() as conn:
        note_id, = conn.execute("INSERT INTO notes (body) VALUES (%s) RETURNING id", (text,)).fetchone()
    return jsonify(id=note_id, text=text), 201


@app.get("/notes/<int:note_id>")
def read_note(note_id):
    key = f"notes:{note_id}"
    text = cache.get(key)
    if text is None:
        with database() as conn:
            row = conn.execute("SELECT body FROM notes WHERE id = %s", (note_id,)).fetchone()
        if row is None:
            abort(404)
        text, = row
        cache.set(key, text, ex=120)
    return jsonify(id=note_id, text=text)


@app.post("/jobs")
def submit_job():
    body = request.get_json(silent=True) or {}
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or len(text) > 4000:
        abort(400, "text must be a string of at most 4000 characters")
    job = word_count.delay(text)
    return jsonify(id=job.id), 202


@app.get("/jobs/<uuid:job_id>")
def job_status(job_id):
    result = queue.AsyncResult(str(job_id))
    return jsonify(id=str(job_id), state=result.state, words=result.result if result.successful() else None)
