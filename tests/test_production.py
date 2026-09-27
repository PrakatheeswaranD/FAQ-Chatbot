import os
import sys
import tempfile
import unittest
import uuid
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("DATABASE_PATH", str(
    Path(tempfile.gettempdir()) / f"kit_faq_test_{os.getpid()}.db"
))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")

import app as chatbot
from fact_check import compose_faq_answer, is_faq_only_answer


class ProductionRouteTests(unittest.TestCase):

    def setUp(self):
        chatbot.app.config["TESTING"] = True
        chatbot.limiter.enabled = False
        self.client = chatbot.app.test_client()

    def test_liveness_and_readiness(self):
        health = self.client.get("/health")
        ready = self.client.get("/ready")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertGreater(ready.get_json()["published_faqs"], 0)

    def test_security_and_request_id_headers(self):
        response = self.client.get("/", headers={"X-Request-ID": "test-request-1"})
        self.assertEqual(response.headers["X-Request-ID"], "test-request-1")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])

    def test_cross_origin_state_change_is_rejected(self):
        response = self.client.post(
            "/chat",
            json={"question": "Library hours?"},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_metrics_endpoint_requires_bearer_token(self):
        self.assertEqual(self.client.get("/metrics").status_code, 404)
        with patch.object(chatbot, "METRICS_TOKEN", "test-metrics-token"):
            response = self.client.get(
                "/metrics",
                headers={"Authorization": "Bearer test-metrics-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"faq_chatbot_requests_total", response.data)
        self.assertIn(b"faq_chatbot_model_errors_total", response.data)


class AdminTests(unittest.TestCase):

    def setUp(self):
        chatbot.app.config["TESTING"] = True
        chatbot.limiter.enabled = False
        self.auth_patchers = [
            patch.object(chatbot, "ADMIN_USERNAME", "admin"),
            patch.object(chatbot, "ADMIN_PASSWORD", "test-password"),
            patch.object(chatbot, "ADMIN_PASSWORD_HASH", None),
        ]
        for auth_patcher in self.auth_patchers:
            auth_patcher.start()
            self.addCleanup(auth_patcher.stop)
        self.client = chatbot.app.test_client()

    def login(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        return self.client.post("/admin/login", data={
            "csrf_token": token,
            "username": "admin",
            "password": "test-password",
        })

    def csrf(self):
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def test_admin_requires_login(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_login_rejects_invalid_password(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        response = self.client.post("/admin/login", data={
            "csrf_token": token,
            "username": "admin",
            "password": "wrong-password",
        })
        self.assertEqual(response.status_code, 401)

    def test_create_edit_version_and_archive_faq(self):
        self.assertEqual(self.login().status_code, 302)
        suffix = uuid.uuid4().hex[:8]
        question = f"Production test question {suffix}?"
        create = self.client.post("/admin/faqs/new", data={
            "csrf_token": self.csrf(),
            "question": question,
            "answer": "Stored production answer.",
            "answer_ta": "சேமிக்கப்பட்ட பதில்.",
            "category": "tests",
            "status": "draft",
            "last_verified": "2026-09-27",
            "action": "save",
        })
        self.assertEqual(create.status_code, 302)
        faq_id = create.headers["Location"].split("/")[-2]
        faq = chatbot.storage.get_faq(faq_id)
        self.assertEqual(faq["status"], "draft")

        update = self.client.post(f"/admin/faqs/{faq_id}/edit", data={
            "csrf_token": self.csrf(),
            "question": question,
            "answer": "Updated stored production answer.",
            "answer_ta": "புதுப்பிக்கப்பட்ட பதில்.",
            "category": "tests",
            "status": "published",
            "last_verified": "2026-09-27",
            "action": "save",
        })
        self.assertEqual(update.status_code, 302)
        self.assertEqual(chatbot.storage.get_faq(faq_id)["version"], 2)
        versions = chatbot.storage.list_faq_versions(faq_id)
        self.assertEqual(len(versions), 1)

        rollback = self.client.post(
            f"/admin/faqs/{faq_id}/rollback/{versions[0]['id']}",
            data={"csrf_token": self.csrf()},
        )
        self.assertEqual(rollback.status_code, 302)
        self.assertEqual(
            chatbot.storage.get_faq(faq_id)["answer"],
            "Stored production answer."
        )

        archive = self.client.post(f"/admin/faqs/{faq_id}/archive", data={
            "csrf_token": self.csrf(),
        })
        self.assertEqual(archive.status_code, 302)
        self.assertEqual(chatbot.storage.get_faq(faq_id)["status"], "archived")

    def test_export_returns_governed_schema(self):
        self.login()
        response = self.client.get("/admin/faqs/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        first = response.get_json()[0]
        self.assertIn("id", first)
        self.assertIn("status", first)
        self.assertIn("last_verified", first)

    def test_import_validation_does_not_mutate_catalogue(self):
        self.login()
        before = len(chatbot.storage.list_all_faqs())
        content = json.dumps([{
            "id": "faq_import_validation",
            "question": "Import validation question?",
            "answer": "Stored import answer.",
            "category": "tests",
            "status": "draft",
            "last_verified": "2026-09-27",
        }])
        response = self.client.post("/admin/faqs/import", data={
            "csrf_token": self.csrf(),
            "content": content,
            "action": "validate",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Valid import", response.data)
        self.assertEqual(len(chatbot.storage.list_all_faqs()), before)


class StoredLanguageTests(unittest.TestCase):

    def test_tamil_answer_is_stored_text_only(self):
        faq = {
            "answer": "Stored English answer.",
            "answer_ta": "சேமிக்கப்பட்ட தமிழ் பதில்.",
        }
        answer = compose_faq_answer([faq], language="ta")
        self.assertEqual(answer, faq["answer_ta"])
        self.assertTrue(is_faq_only_answer(answer, [faq], language="ta"))

    def test_missing_tamil_variant_falls_back_to_stored_english(self):
        faq = {"answer": "Stored English answer.", "answer_ta": None}
        self.assertEqual(compose_faq_answer([faq], language="ta"), faq["answer"])


class CircuitBreakerTests(unittest.TestCase):

    def test_open_circuit_fails_without_network_call(self):
        with patch.object(chatbot, "_circuit_open_until", float("inf")):
            with self.assertRaises(chatbot.ModelUnavailable):
                chatbot.generate_answer("hello")


class BackupTests(unittest.TestCase):

    def test_online_database_backup_is_readable(self):
        destination = Path(tempfile.mkdtemp()) / "backup.db"
        chatbot.storage.backup_database(destination)
        self.assertTrue(destination.exists())
        with sqlite3.connect(destination) as connection:
            count = connection.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
