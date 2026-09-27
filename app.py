import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import APIError, Groq
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import storage
from fact_check import compose_faq_answer, is_faq_only_answer
from language import build_translation_prompt, is_tamil_script, needs_translation
from prompt import FALLBACK_ANSWER, build_prompt
from retrieval import FaqRetriever


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
MAX_QUESTION_LENGTH = int(os.getenv("MAX_QUESTION_LENGTH", "500"))
CHAT_RATE_LIMIT = os.getenv("CHAT_RATE_LIMIT", "10 per minute")
FEEDBACK_RATE_LIMIT = os.getenv("FEEDBACK_RATE_LIMIT", "30 per minute")
ADMIN_RATE_LIMIT = os.getenv("ADMIN_RATE_LIMIT", "20 per minute")
RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
TRUST_PROXY_COUNT = int(os.getenv("TRUST_PROXY_COUNT", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
MODEL_FAILURE_THRESHOLD = int(os.getenv("MODEL_FAILURE_THRESHOLD", "3"))
MODEL_COOLDOWN_SECONDS = int(os.getenv("MODEL_COOLDOWN_SECONDS", "30"))
METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logger = logging.getLogger("faq-chatbot")


def validate_configuration():
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    secret_key = os.getenv("SECRET_KEY", "")
    if IS_PRODUCTION and (
        len(secret_key) < 32 or secret_key.startswith("replace-")
    ):
        missing.append("SECRET_KEY")
    if IS_PRODUCTION and (
        not ADMIN_PASSWORD_HASH
        or "$" not in ADMIN_PASSWORD_HASH
        or ADMIN_PASSWORD_HASH.startswith("replace-")
    ):
        missing.append("ADMIN_PASSWORD_HASH")
    if IS_PRODUCTION and (
        len(METRICS_TOKEN) < 24 or METRICS_TOKEN.startswith("replace-")
    ):
        missing.append("METRICS_TOKEN")
    if missing:
        raise ValueError("Missing required configuration: " + ", ".join(missing))
    if IS_PRODUCTION and RATELIMIT_STORAGE_URI == "memory://":
        logger.warning("Production is using an in-memory rate limiter; configure Redis for multiple processes")


validate_configuration()
storage.initialize_database()

groq_client = Groq(api_key=GROQ_API_KEY, timeout=30, max_retries=2)

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=16 * 1024,
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=1800,
)
if TRUST_PROXY_COUNT:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=TRUST_PROXY_COUNT,
        x_proto=TRUST_PROXY_COUNT,
        x_host=TRUST_PROXY_COUNT,
    )

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=RATELIMIT_STORAGE_URI,
    default_limits=[os.getenv("GLOBAL_RATE_LIMIT", "120 per minute")],
)


_faq_cache_lock = threading.RLock()
_faq_cache_revision = None
FAQS = []
RETRIEVER = FaqRetriever([])


def load_faqs():
    return storage.list_published_faqs()


def get_faqs():
    global FAQS, RETRIEVER, _faq_cache_revision
    revision = storage.get_faq_revision()
    with _faq_cache_lock:
        if revision != _faq_cache_revision:
            FAQS = load_faqs()
            RETRIEVER = FaqRetriever(FAQS)
            _faq_cache_revision = revision
            logger.info("Loaded %d published FAQs at revision %s", len(FAQS), revision)
    return FAQS


get_faqs()


_circuit_lock = threading.Lock()
_model_failures = 0
_circuit_open_until = 0.0
_metrics_lock = threading.Lock()
_metrics = {
    "requests_total": {},
    "request_duration_seconds_sum": 0.0,
    "model_errors_total": 0,
    "fallbacks_total": {},
}


class ModelUnavailable(Exception):
    pass


def generate_answer(messages, json_mode=False):
    global _model_failures, _circuit_open_until
    with _circuit_lock:
        if time.monotonic() < _circuit_open_until:
            raise ModelUnavailable("Model circuit breaker is open")

    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    options = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 256,
    }
    if json_mode:
        options["response_format"] = {"type": "json_object"}

    try:
        response = groq_client.chat.completions.create(**options)
    except APIError:
        with _circuit_lock:
            _model_failures += 1
            if _model_failures >= MODEL_FAILURE_THRESHOLD:
                _circuit_open_until = time.monotonic() + MODEL_COOLDOWN_SECONDS
        with _metrics_lock:
            _metrics["model_errors_total"] += 1
        raise

    with _circuit_lock:
        _model_failures = 0
        _circuit_open_until = 0.0
    return response.choices[0].message.content


def translate_question(question):
    if not needs_translation(question):
        return None
    english = (generate_answer(build_translation_prompt(question)) or "").strip()
    return english[:MAX_QUESTION_LENGTH] or None


def parse_faq_selection(raw_selection, candidate_count):
    try:
        data = json.loads((raw_selection or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != {"answerable", "faq_ids"}:
        return None
    if not isinstance(data["answerable"], bool) or not isinstance(data["faq_ids"], list):
        return None
    faq_ids = data["faq_ids"]
    if not data["answerable"]:
        return [] if faq_ids == [] else None
    if not faq_ids:
        return None

    selected = []
    seen = set()
    for faq_id in faq_ids:
        if isinstance(faq_id, bool) or not isinstance(faq_id, int):
            return None
        if faq_id < 1 or faq_id > candidate_count or faq_id in seen:
            return None
        seen.add(faq_id)
        selected.append(faq_id - 1)
    return selected


def log_unanswered(question, reason, model_output=None):
    try:
        storage.log_unanswered(question, reason, model_output)
    except sqlite3.Error:
        logger.exception("Could not persist unanswered question")


def answer_question(question):
    get_faqs()
    english = translate_question(question)
    matched_faqs = RETRIEVER.search(f"{question} {english}" if english else question)
    if not matched_faqs:
        log_unanswered(question, "no_matching_faq")
        with _metrics_lock:
            _metrics["fallbacks_total"]["no_matching_faq"] = (
                _metrics["fallbacks_total"].get("no_matching_faq", 0) + 1
            )
        return FALLBACK_ANSWER, "no_match", []

    messages = build_prompt(matched_faqs, question, english)
    model_output = (generate_answer(messages, json_mode=True) or "").strip()
    selected_indexes = parse_faq_selection(model_output, len(matched_faqs))
    if selected_indexes is None:
        logger.warning("Rejected invalid FAQ selection")
        log_unanswered(question, "invalid_selection", model_output[:1000])
        with _metrics_lock:
            _metrics["fallbacks_total"]["invalid_selection"] = (
                _metrics["fallbacks_total"].get("invalid_selection", 0) + 1
            )
        return FALLBACK_ANSWER, "blocked", []
    if not selected_indexes:
        log_unanswered(question, "model_fallback")
        with _metrics_lock:
            _metrics["fallbacks_total"]["model_fallback"] = (
                _metrics["fallbacks_total"].get("model_fallback", 0) + 1
            )
        return FALLBACK_ANSWER, "fallback", []

    selected_faqs = [matched_faqs[index] for index in selected_indexes]
    language = "ta" if is_tamil_script(question) else "en"
    answer = compose_faq_answer(selected_faqs, language=language)
    if not is_faq_only_answer(answer, selected_faqs, language=language):
        raise AssertionError("FAQ-only answer construction failed")
    return answer, "answered", [faq["question"] for faq in selected_faqs]


def remember_answer(question, answer, status, sources):
    answer_id = uuid.uuid4().hex
    storage.remember_answer(answer_id, question, answer, status, sources)
    return answer_id


def log_feedback(answer_id, rating):
    return storage.save_feedback(answer_id, rating)


def _safe_request_id(value):
    if value and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", value):
        return value
    return uuid.uuid4().hex


def _origin_allowed():
    origin = request.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.netloc == request.host and parsed.scheme in {"http", "https"}


@app.before_request
def before_request():
    g.request_id = _safe_request_id(request.headers.get("X-Request-ID"))
    g.started_at = time.perf_counter()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_allowed():
        return jsonify({"error": "Request origin is not allowed."}), 403


@app.after_request
def add_security_headers(response):
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    )
    if IS_PRODUCTION or request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith(("/chat", "/feedback", "/admin")):
        response.headers["Cache-Control"] = "no-store"
    elapsed_ms = round((time.perf_counter() - getattr(g, "started_at", time.perf_counter())) * 1000, 2)
    with _metrics_lock:
        key = (request.method, request.path, response.status_code)
        _metrics["requests_total"][key] = _metrics["requests_total"].get(key, 0) + 1
        _metrics["request_duration_seconds_sum"] += elapsed_ms / 1000
    logger.info(
        "%s %s %s %.2fms",
        request.method,
        request.path,
        response.status_code,
        elapsed_ms,
        extra={"request_id": getattr(g, "request_id", None)},
    )
    return response


@app.errorhandler(429)
def rate_limited(_error):
    return jsonify({"error": "Too many requests. Please wait and try again."}), 429


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Request is too large."}), 413


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
@limiter.exempt
def health():
    return jsonify({"status": "ok", "environment": APP_ENV})


@app.route("/ready")
@limiter.exempt
def ready():
    database_ready, published = storage.readiness()
    rate_limit_ready = True
    if RATELIMIT_STORAGE_URI.startswith(("redis://", "rediss://")):
        try:
            import redis
            rate_limit_ready = bool(
                redis.Redis.from_url(RATELIMIT_STORAGE_URI, socket_timeout=2).ping()
            )
        except Exception:
            rate_limit_ready = False
    ready_now = database_ready and bool(GROQ_API_KEY) and rate_limit_ready
    return jsonify({
        "status": "ready" if ready_now else "not_ready",
        "database": "ok" if database_ready else "unavailable",
        "published_faqs": published,
        "model_configured": bool(GROQ_API_KEY),
        "rate_limit_storage": "ok" if rate_limit_ready else "unavailable",
    }), 200 if ready_now else 503


@app.route("/metrics")
@limiter.exempt
def metrics():
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {METRICS_TOKEN}" if METRICS_TOKEN else ""
    if not expected or not hmac.compare_digest(authorization, expected):
        return "Not found", 404
    with _metrics_lock:
        snapshot = {
            "requests_total": dict(_metrics["requests_total"]),
            "request_duration_seconds_sum": _metrics["request_duration_seconds_sum"],
            "model_errors_total": _metrics["model_errors_total"],
            "fallbacks_total": dict(_metrics["fallbacks_total"]),
        }
    lines = [
        "# HELP faq_chatbot_requests_total HTTP responses by method, path and status.",
        "# TYPE faq_chatbot_requests_total counter",
    ]
    for (method, path, status), value in sorted(snapshot["requests_total"].items()):
        safe_path = path.replace('"', '')
        lines.append(
            f'faq_chatbot_requests_total{{method="{method}",path="{safe_path}",status="{status}"}} {value}'
        )
    lines.extend([
        "# TYPE faq_chatbot_request_duration_seconds_sum counter",
        f"faq_chatbot_request_duration_seconds_sum {snapshot['request_duration_seconds_sum']:.6f}",
        "# TYPE faq_chatbot_model_errors_total counter",
        f"faq_chatbot_model_errors_total {snapshot['model_errors_total']}",
        "# TYPE faq_chatbot_fallbacks_total counter",
    ])
    for reason, value in sorted(snapshot["fallbacks_total"].items()):
        lines.append(f'faq_chatbot_fallbacks_total{{reason="{reason}"}} {value}')
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.route("/chat", methods=["POST"])
@limiter.limit(CHAT_RATE_LIMIT)
def chat():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "No data received."}), 400
    question = str(data.get("question", "")).strip()
    if not question:
        return jsonify({"error": "Please enter a question."}), 400
    if len(question) > MAX_QUESTION_LENGTH:
        return jsonify({"error": f"Please keep your question under {MAX_QUESTION_LENGTH} characters."}), 400
    try:
        answer, status, sources = answer_question(question)
        return jsonify({
            "id": remember_answer(question, answer, status, sources),
            "answer": answer,
            "sources": sources,
        })
    except (APIError, ModelUnavailable):
        logger.exception("Model service unavailable")
        return jsonify({"error": "The chatbot is busy right now. Please try again shortly."}), 503
    except sqlite3.Error:
        logger.exception("Database error")
        return jsonify({"error": "The chatbot is temporarily unavailable."}), 503
    except Exception:
        logger.exception("Chat error")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


@app.route("/feedback", methods=["POST"])
@limiter.limit(FEEDBACK_RATE_LIMIT)
def feedback():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "No data received."}), 400
    answer_id = str(data.get("id", ""))
    rating = data.get("rating")
    if rating not in ("up", "down") or not re.fullmatch(r"[0-9a-f]{32}", answer_id):
        return jsonify({"error": "Invalid feedback."}), 400
    if not log_feedback(answer_id, rating):
        return jsonify({"error": "This answer has expired."}), 404
    return jsonify({"status": "saved"})


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf():
    submitted = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(submitted, expected):
        return False
    return True


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _admin_password_valid(password):
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, password)
    if not IS_PRODUCTION and ADMIN_PASSWORD:
        return hmac.compare_digest(ADMIN_PASSWORD, password)
    return False


def _parse_optional_date(value, field_name):
    value = (value or "").strip()
    if not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO date or datetime") from error
    return value


def validate_faq_data(data):
    if not isinstance(data, dict):
        raise ValueError("Every FAQ must be a JSON object")
    question = str(data.get("question", "")).strip()
    answer = str(data.get("answer", "")).strip()
    answer_ta = str(data.get("answer_ta", "")).strip() or None
    category = str(data.get("category", "general")).strip().lower()
    status = str(data.get("status", "draft")).strip().lower()
    if not question or len(question) > 500:
        raise ValueError("Question is required and must be at most 500 characters")
    if not answer or len(answer) > 5000:
        raise ValueError("English answer is required and must be at most 5,000 characters")
    if answer_ta and len(answer_ta) > 5000:
        raise ValueError("Tamil answer must be at most 5,000 characters")
    if not re.fullmatch(r"[a-z0-9_-]{1,50}", category):
        raise ValueError("Category may contain lowercase letters, numbers, underscores and hyphens")
    if status not in {"draft", "published", "archived"}:
        raise ValueError("Status must be draft, published or archived")
    faq_id = str(data.get("id", "")).strip() or None
    if faq_id and not re.fullmatch(r"faq_[a-zA-Z0-9_-]{4,64}", faq_id):
        raise ValueError("FAQ id must start with faq_ and contain only safe characters")
    return {
        "id": faq_id,
        "question": question,
        "answer": answer,
        "answer_ta": answer_ta,
        "category": category,
        "status": status,
        "effective_from": _parse_optional_date(data.get("effective_from"), "Effective from"),
        "last_verified": _parse_optional_date(data.get("last_verified"), "Last verified"),
    }


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit(ADMIN_RATE_LIMIT)
def admin_login():
    if request.method == "POST":
        if not require_csrf():
            return render_template("admin_login.html", error="Invalid session token."), 400
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username, ADMIN_USERNAME) and _admin_password_valid(password):
            session.clear()
            session["admin_authenticated"] = True
            session["admin_username"] = username
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        time.sleep(0.25)
        return render_template("admin_login.html", error="Invalid credentials."), 401
    configured = bool(ADMIN_PASSWORD_HASH or (not IS_PRODUCTION and ADMIN_PASSWORD))
    return render_template("admin_login.html", configured=configured)


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    if not require_csrf():
        return "Invalid session token", 400
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template(
        "admin_dashboard.html",
        faqs=storage.list_all_faqs(),
        stats=storage.dashboard_stats(),
        unanswered=storage.list_unanswered(20),
        feedback=storage.list_feedback(20),
        audit=storage.list_audit(20),
    )


def _faq_form_data():
    return {
        "question": request.form.get("question", ""),
        "answer": request.form.get("answer", ""),
        "answer_ta": request.form.get("answer_ta", ""),
        "category": request.form.get("category", "general"),
        "status": request.form.get("status", "draft"),
        "effective_from": request.form.get("effective_from", ""),
        "last_verified": request.form.get("last_verified", ""),
    }


@app.route("/admin/faqs/new", methods=["GET", "POST"])
@admin_required
def admin_faq_new():
    faq = _faq_form_data() if request.method == "POST" else {"status": "draft", "category": "general"}
    if request.method == "POST":
        if not require_csrf():
            return "Invalid session token", 400
        try:
            validated = validate_faq_data(faq)
            if request.form.get("action") == "preview":
                return render_template("admin_faq_form.html", faq=validated, preview=True, versions=[])
            faq_id = storage.create_faq(validated, session["admin_username"])
            flash("FAQ created.", "success")
            return redirect(url_for("admin_faq_edit", faq_id=faq_id))
        except (ValueError, sqlite3.IntegrityError) as error:
            return render_template("admin_faq_form.html", faq=faq, error=str(error), versions=[]), 400
    return render_template("admin_faq_form.html", faq=faq, versions=[])


@app.route("/admin/faqs/<faq_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_faq_edit(faq_id):
    existing = storage.get_faq(faq_id)
    if existing is None:
        return "FAQ not found", 404
    faq = _faq_form_data() if request.method == "POST" else existing
    if request.method == "POST":
        if not require_csrf():
            return "Invalid session token", 400
        try:
            validated = validate_faq_data(faq)
            if request.form.get("action") == "preview":
                return render_template(
                    "admin_faq_form.html", faq=validated, preview=True,
                    versions=storage.list_faq_versions(faq_id)
                )
            storage.update_faq(faq_id, validated, session["admin_username"])
            flash("FAQ saved and cache revision updated.", "success")
            return redirect(url_for("admin_faq_edit", faq_id=faq_id))
        except (ValueError, sqlite3.IntegrityError) as error:
            return render_template(
                "admin_faq_form.html", faq=faq, error=str(error),
                versions=storage.list_faq_versions(faq_id)
            ), 400
    return render_template(
        "admin_faq_form.html", faq=faq,
        versions=storage.list_faq_versions(faq_id)
    )


@app.route("/admin/faqs/<faq_id>/archive", methods=["POST"])
@admin_required
def admin_faq_archive(faq_id):
    if not require_csrf():
        return "Invalid session token", 400
    faq = storage.get_faq(faq_id)
    if faq is None:
        return "FAQ not found", 404
    faq["status"] = "archived"
    storage.update_faq(faq_id, faq, session["admin_username"], action="archive_faq")
    flash("FAQ archived.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/faqs/<faq_id>/rollback/<int:version_id>", methods=["POST"])
@admin_required
def admin_faq_rollback(faq_id, version_id):
    if not require_csrf():
        return "Invalid session token", 400
    if not storage.rollback_faq(faq_id, version_id, session["admin_username"]):
        return "Version not found", 404
    flash("FAQ rolled back. The replaced version remains in history.", "success")
    return redirect(url_for("admin_faq_edit", faq_id=faq_id))


@app.route("/admin/faqs/export")
@admin_required
def admin_faq_export():
    payload = json.dumps(storage.export_faqs(), ensure_ascii=False, indent=2)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=kit-faq-export.json"},
    )


@app.route("/admin/faqs/import", methods=["GET", "POST"])
@admin_required
def admin_faq_import():
    content = request.form.get("content", "") if request.method == "POST" else ""
    if request.method == "POST":
        if not require_csrf():
            return "Invalid session token", 400
        try:
            raw = json.loads(content)
            if not isinstance(raw, list) or not raw:
                raise ValueError("Import must be a non-empty JSON array")
            validated = [validate_faq_data(item) for item in raw]
            questions = [item["question"].casefold() for item in validated]
            if len(questions) != len(set(questions)):
                raise ValueError("Import contains duplicate questions")
            if request.form.get("action") == "validate":
                return render_template(
                    "admin_import.html", content=content,
                    success=f"Valid import: {len(validated)} FAQs. No changes made."
                )
            if request.form.get("confirm") != "REPLACE":
                raise ValueError("Type REPLACE to apply the import")
            storage.replace_faqs(validated, session["admin_username"])
            flash(f"Imported {len(validated)} FAQs.", "success")
            return redirect(url_for("admin_dashboard"))
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError) as error:
            return render_template("admin_import.html", content=content, error=str(error)), 400
    return render_template("admin_import.html", content=content)


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1" and not IS_PRODUCTION,
    )
