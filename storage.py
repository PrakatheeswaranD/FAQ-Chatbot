import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "data" / "faq_chatbot.db"))
FAQ_SEED_PATH = BASE_DIR / "data" / "faq.json"
MAX_STORED_ANSWERS = int(os.getenv("MAX_STORED_ANSWERS", "10000"))


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize_database():
    with _connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS faqs (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer_en TEXT NOT NULL,
                answer_ta TEXT,
                category TEXT NOT NULL DEFAULT 'general',
                status TEXT NOT NULL DEFAULT 'published'
                    CHECK (status IN ('draft', 'published', 'archived')),
                effective_from TEXT,
                last_verified TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS faqs_question_unique
                ON faqs(lower(question));
            CREATE INDEX IF NOT EXISTS faqs_status_index ON faqs(status);

            CREATE TABLE IF NOT EXISTS faq_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                faq_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unanswered (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                question TEXT NOT NULL,
                reason TEXT NOT NULL,
                model_output TEXT
            );

            CREATE TABLE IF NOT EXISTS answers (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                status TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                rating TEXT CHECK (rating IS NULL OR rating IN ('up', 'down'))
            );

            CREATE INDEX IF NOT EXISTS answers_created_index
                ON answers(created_at DESC);

            CREATE TABLE IF NOT EXISTS feedback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                answer_id TEXT NOT NULL,
                rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                status TEXT NOT NULL,
                sources_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target_id TEXT,
                details_json TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
            (utc_now(),)
        )
    seed_faqs_if_empty()


def _stable_seed_id(question):
    digest = hashlib.sha256(question.casefold().encode("utf-8")).hexdigest()[:12]
    return f"faq_{digest}"


def _infer_category(question):
    text = question.casefold()
    categories = {
        "admissions": ("admission", "tnea", "eligibility", "course"),
        "fees": ("fee", "payment", "scholarship"),
        "hostel": ("hostel", "warden", "mess"),
        "examinations": ("exam", "result", "hall ticket", "revaluation", "attendance"),
        "placements": ("placement", "company", "internship"),
        "transport": ("bus", "transport", "railway", "distance"),
        "campus": ("library", "wi-fi", "canteen", "gym", "sports", "medical"),
        "contacts": ("contact", "principal", "ceo", "office"),
    }
    for category, keywords in categories.items():
        if any(keyword in text for keyword in keywords):
            return category
    return "general"


def seed_faqs_if_empty():
    with _connect() as connection:
        if connection.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]:
            return
        with open(FAQ_SEED_PATH, encoding="utf-8") as file:
            seed = json.load(file)
        now = utc_now()
        for faq in seed:
            connection.execute(
                """
                INSERT INTO faqs(
                    id, question, answer_en, answer_ta, category, status,
                    effective_from, last_verified, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'published', ?, ?, 1, ?, ?)
                """,
                (
                    faq.get("id") or _stable_seed_id(faq["question"]),
                    faq["question"].strip(),
                    faq["answer"].strip(),
                    (faq.get("answer_ta") or "").strip() or None,
                    faq.get("category") or _infer_category(faq["question"]),
                    faq.get("effective_from"),
                    faq.get("last_verified") or now[:10],
                    now,
                    now,
                )
            )
        _audit(connection, "system", "seed_faqs", None, {"count": len(seed)})


def _faq_from_row(row):
    return {
        "id": row["id"],
        "question": row["question"],
        "answer": row["answer_en"],
        "answer_ta": row["answer_ta"],
        "category": row["category"],
        "status": row["status"],
        "effective_from": row["effective_from"],
        "last_verified": row["last_verified"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_published_faqs():
    now = utc_now()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM faqs
            WHERE status = 'published'
              AND (effective_from IS NULL OR effective_from = '' OR effective_from <= ?)
            ORDER BY category, question
            """,
            (now,)
        ).fetchall()
    return [_faq_from_row(row) for row in rows]


def list_all_faqs():
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM faqs ORDER BY status DESC, category, question"
        ).fetchall()
    return [_faq_from_row(row) for row in rows]


def get_faq(faq_id):
    with _connect() as connection:
        row = connection.execute("SELECT * FROM faqs WHERE id = ?", (faq_id,)).fetchone()
    return _faq_from_row(row) if row else None


def get_faq_revision():
    with _connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS updated FROM faqs"
        ).fetchone()
    return f"{row['count']}:{row['updated']}"


def _snapshot(faq):
    return json.dumps(faq, ensure_ascii=False, sort_keys=True)


def _audit(connection, actor, action, target_id, details):
    connection.execute(
        """
        INSERT INTO admin_audit(created_at, actor, action, target_id, details_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (utc_now(), actor, action, target_id, json.dumps(details, ensure_ascii=False))
    )


def create_faq(data, actor):
    faq_id = data.get("id") or f"faq_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO faqs(
                id, question, answer_en, answer_ta, category, status,
                effective_from, last_verified, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                faq_id, data["question"], data["answer"], data.get("answer_ta"),
                data["category"], data["status"], data.get("effective_from"),
                data.get("last_verified"), now, now,
            )
        )
        _audit(connection, actor, "create_faq", faq_id, {"version": 1})
    return faq_id


def update_faq(faq_id, data, actor, action="update_faq"):
    now = utc_now()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM faqs WHERE id = ?", (faq_id,)).fetchone()
        if row is None:
            return False
        old = _faq_from_row(row)
        connection.execute(
            """
            INSERT INTO faq_versions(faq_id, version, snapshot_json, changed_at, changed_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (faq_id, old["version"], _snapshot(old), now, actor)
        )
        connection.execute(
            """
            UPDATE faqs SET
                question = ?, answer_en = ?, answer_ta = ?, category = ?,
                status = ?, effective_from = ?, last_verified = ?,
                version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (
                data["question"], data["answer"], data.get("answer_ta"),
                data["category"], data["status"], data.get("effective_from"),
                data.get("last_verified"), now, faq_id,
            )
        )
        _audit(connection, actor, action, faq_id, {"from_version": old["version"]})
    return True


def list_faq_versions(faq_id):
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, faq_id, version, snapshot_json, changed_at, changed_by
            FROM faq_versions WHERE faq_id = ? ORDER BY id DESC
            """,
            (faq_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def rollback_faq(faq_id, version_id, actor):
    with _connect() as connection:
        version = connection.execute(
            "SELECT snapshot_json FROM faq_versions WHERE id = ? AND faq_id = ?",
            (version_id, faq_id)
        ).fetchone()
    if version is None:
        return False
    snapshot = json.loads(version["snapshot_json"])
    return update_faq(faq_id, snapshot, actor, action="rollback_faq")


def export_faqs():
    return [
        {
            "id": faq["id"],
            "question": faq["question"],
            "answer": faq["answer"],
            "answer_ta": faq["answer_ta"],
            "category": faq["category"],
            "status": faq["status"],
            "effective_from": faq["effective_from"],
            "last_verified": faq["last_verified"],
        }
        for faq in list_all_faqs()
    ]


def replace_faqs(faqs, actor):
    now = utc_now()
    incoming_ids = set()
    with _connect() as connection:
        for data in faqs:
            faq_id = data.get("id") or _stable_seed_id(data["question"])
            if faq_id in incoming_ids:
                raise ValueError(f"Duplicate FAQ id: {faq_id}")
            incoming_ids.add(faq_id)
            existing = connection.execute(
                "SELECT * FROM faqs WHERE id = ?", (faq_id,)
            ).fetchone()
            if existing:
                old = _faq_from_row(existing)
                connection.execute(
                    """
                    INSERT INTO faq_versions(
                        faq_id, version, snapshot_json, changed_at, changed_by
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (faq_id, old["version"], _snapshot(old), now, actor)
                )
                connection.execute(
                    """
                    UPDATE faqs SET question=?, answer_en=?, answer_ta=?, category=?,
                        status=?, effective_from=?, last_verified=?, version=version+1,
                        updated_at=? WHERE id=?
                    """,
                    (
                        data["question"], data["answer"], data.get("answer_ta"),
                        data["category"], data["status"], data.get("effective_from"),
                        data.get("last_verified"), now, faq_id,
                    )
                )
            else:
                connection.execute(
                    """
                    INSERT INTO faqs(
                        id, question, answer_en, answer_ta, category, status,
                        effective_from, last_verified, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        faq_id, data["question"], data["answer"], data.get("answer_ta"),
                        data["category"], data["status"], data.get("effective_from"),
                        data.get("last_verified"), now, now,
                    )
                )

        existing_ids = {
            row[0] for row in connection.execute("SELECT id FROM faqs").fetchall()
        }
        for removed_id in existing_ids - incoming_ids:
            connection.execute(
                "UPDATE faqs SET status='archived', version=version+1, updated_at=? WHERE id=?",
                (now, removed_id)
            )
        _audit(connection, actor, "replace_faqs", None, {"count": len(faqs)})


def log_unanswered(question, reason, model_output=None):
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO unanswered(created_at, question, reason, model_output)
            VALUES (?, ?, ?, ?)
            """,
            (utc_now(), question, reason, model_output)
        )


def remember_answer(answer_id, question, answer, status, sources):
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO answers(id, created_at, question, answer, status, sources_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (answer_id, utc_now(), question, answer, status, json.dumps(sources))
        )
        connection.execute(
            """
            DELETE FROM answers WHERE id IN (
                SELECT id FROM answers ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (MAX_STORED_ANSWERS,)
        )


def save_feedback(answer_id, rating):
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        answer = connection.execute(
            "SELECT * FROM answers WHERE id = ?", (answer_id,)
        ).fetchone()
        if answer is None:
            return False
        if answer["rating"] == rating:
            return True
        connection.execute(
            "UPDATE answers SET rating = ? WHERE id = ?", (rating, answer_id)
        )
        connection.execute(
            """
            INSERT INTO feedback_events(
                created_at, answer_id, rating, question, answer, status, sources_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(), answer_id, rating, answer["question"], answer["answer"],
                answer["status"], answer["sources_json"],
            )
        )
    return True


def list_unanswered(limit=100):
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM unanswered ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def list_feedback(limit=100):
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM feedback_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["sources"] = json.loads(item.pop("sources_json"))
        result.append(item)
    return result


def dashboard_stats():
    with _connect() as connection:
        faq_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM faqs GROUP BY status"
            )
        }
        unanswered = connection.execute("SELECT COUNT(*) FROM unanswered").fetchone()[0]
        feedback = {
            row["rating"]: row["count"]
            for row in connection.execute(
                """
                SELECT rating, COUNT(*) AS count FROM answers
                WHERE rating IS NOT NULL GROUP BY rating
                """
            )
        }
    return {
        "published": faq_counts.get("published", 0),
        "draft": faq_counts.get("draft", 0),
        "archived": faq_counts.get("archived", 0),
        "unanswered": unanswered,
        "helpful": feedback.get("up", 0),
        "not_helpful": feedback.get("down", 0),
    }


def list_audit(limit=100):
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def readiness():
    try:
        with _connect() as connection:
            connection.execute("SELECT 1").fetchone()
            published = connection.execute(
                "SELECT COUNT(*) FROM faqs WHERE status='published'"
            ).fetchone()[0]
        return published > 0, published
    except sqlite3.Error:
        return False, 0


def backup_database(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination
