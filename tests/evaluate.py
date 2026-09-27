import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DATABASE_PATH"] = str(
    Path(tempfile.gettempdir()) / f"kit_faq_eval_{os.getpid()}.db"
)

import app as chatbot
from groq import APIError

REFUSAL_MARKER = "I don't have information about that in the available FAQs"
DELAY_SECONDS = 3


def normalize(text):
    for char in (" ", " "):
        text = text.replace(char, " ")
    for char in ("‑", "‐", "–"):
        text = text.replace(char, "-")
    return text.replace("’", "'")


def ask(question, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            answer, _status, _sources = chatbot.answer_question(question)
            return answer
        except APIError as error:
            if attempt == attempts:
                return None
            print(f"    (API error: {error}; retrying)")
            time.sleep(10 * attempt)


def check(case, answer):
    answer = normalize(answer)
    refused = REFUSAL_MARKER in answer

    if case["expected_type"] == "not_available":
        return refused, "expected the fallback answer"

    if refused:
        if case.get("fallback_ok"):
            return True, ""
        return False, "refused, but the FAQ has an answer"
    for text in case.get("must_contain", []):
        if text.lower() not in answer.lower():
            return False, f"missing '{text}'"
    for text in case.get("must_not_contain", []):
        if text.lower() in answer.lower():
            return False, f"contains invented '{text}'"
    return True, ""


def main():
    with open(ROOT / "tests" / "test_questions.json", encoding="utf-8") as file:
        categories = json.load(file)

    total_passed = total = 0

    for category, cases in categories.items():
        passed = 0
        print(f"\n== {category} ==")

        for case in cases:
            answer = ask(case["question"])
            if answer is None:
                ok, reason, answer = False, "API unreachable", ""
            else:
                ok, reason = check(case, answer)
            passed += ok
            status = "PASS" if ok else f"FAIL ({reason})"
            print(f"[{status}] {case['question']}")
            if not ok:
                print(f"    answer: {normalize(answer)[:300]}")
            time.sleep(DELAY_SECONDS)

        print(f"-- {passed}/{len(cases)} passed")
        total_passed += passed
        total += len(cases)

    print(f"\nTOTAL: {total_passed}/{total} passed "
          f"({100 * total_passed / total:.0f}%)")
    sys.exit(0 if total_passed == total else 1)


if __name__ == "__main__":
    main()
