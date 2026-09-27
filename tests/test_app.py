import os
import sys
import tempfile
import unittest
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
from prompt import FALLBACK_ANSWER, FEW_SHOT_MESSAGES, build_prompt


class FaqDataTests(unittest.TestCase):

    def test_faqs_have_question_and_answer(self):
        faqs = chatbot.load_faqs()
        self.assertGreater(len(faqs), 0)
        for faq in faqs:
            self.assertTrue(faq["question"].strip())
            self.assertTrue(faq["answer"].strip())

    def test_no_duplicate_questions(self):
        questions = [faq["question"].lower() for faq in chatbot.load_faqs()]
        self.assertEqual(len(questions), len(set(questions)))


class PromptTests(unittest.TestCase):

    def test_prompt_uses_role_separation_and_four_few_shot_examples(self):
        messages = build_prompt(
            [{"question": "Q1?", "answer": "A1."}],
            "What is Q1?"
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(
            sum(message["role"] == "assistant" for message in FEW_SHOT_MESSAGES),
            4
        )

    def test_current_candidates_and_question_are_present(self):
        messages = build_prompt(
            [{"question": "Q1?", "answer": "A1."}],
            "What is Q1?"
        )
        current = messages[-1]["content"]
        self.assertIn("FAQ 1", current)
        self.assertIn("Question: Q1?", current)
        self.assertIn("Answer: A1.", current)
        self.assertIn("What is Q1?", current)

    def test_model_is_told_to_select_not_answer(self):
        system = build_prompt([], "unknown")[0]["content"]
        self.assertIn("You do not write the answer", system)
        self.assertIn('"faq_ids"', system)
        self.assertIn("Never use outside knowledge", system)


class SelectionParserTests(unittest.TestCase):

    def test_valid_selection(self):
        raw = '{"answerable": true, "faq_ids": [2, 1]}'
        self.assertEqual(chatbot.parse_faq_selection(raw, 3), [1, 0])

    def test_valid_refusal(self):
        raw = '{"answerable": false, "faq_ids": []}'
        self.assertEqual(chatbot.parse_faq_selection(raw, 3), [])

    def test_invalid_outputs_are_rejected(self):
        invalid = [
            "not json",
            '{"answerable": true, "faq_ids": []}',
            '{"answerable": false, "faq_ids": [1]}',
            '{"answerable": true, "faq_ids": [0]}',
            '{"answerable": true, "faq_ids": [4]}',
            '{"answerable": true, "faq_ids": [1, 1]}',
            '{"answerable": true, "faq_ids": [true]}',
            '{"answerable": true, "faq_ids": [1], "answer": "invented"}',
        ]
        for raw in invalid:
            self.assertIsNone(chatbot.parse_faq_selection(raw, 3), raw)


class ChatRouteTests(unittest.TestCase):

    def setUp(self):
        chatbot.app.config["TESTING"] = True
        chatbot.limiter.enabled = False
        self.client = chatbot.app.test_client()

    def test_home_page_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"KIT FAQ Chatbot", response.data)

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_empty_question_rejected(self):
        response = self.client.post("/chat", json={"question": "  "})
        self.assertEqual(response.status_code, 400)

    def test_missing_body_rejected(self):
        response = self.client.post("/chat", data="not json")
        self.assertEqual(response.status_code, 400)

    def test_long_question_rejected(self):
        response = self.client.post("/chat", json={"question": "a" * 501})
        self.assertEqual(response.status_code, 400)

    @patch.object(chatbot, "generate_answer",
                  return_value='{"answerable": true, "faq_ids": [1]}')
    def test_answer_is_copied_from_faq_not_model(self, mock_generate):
        response = self.client.post("/chat", json={"question": "Library hours?"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        expected = next(
            faq["answer"] for faq in chatbot.load_faqs()
            if faq["question"] == "What are the library working hours?"
        )
        self.assertEqual(data["answer"], expected)
        self.assertEqual(data["sources"], ["What are the library working hours?"])
        self.assertTrue(data["id"])
        messages = mock_generate.call_args[0][0]
        self.assertEqual(messages[0]["role"], "system")

    @patch.object(chatbot, "generate_answer", side_effect=RuntimeError("boom"))
    def test_unexpected_error_hidden_from_user(self, _mock_generate):
        response = self.client.post("/chat", json={"question": "Library hours?"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("boom", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
