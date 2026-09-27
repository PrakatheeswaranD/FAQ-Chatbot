from collections import Counter

import storage


REASONS = {
    "no_matching_faq": "no FAQ matched the question",
    "model_fallback": "the selector found no answer in the candidates",
    "invalid_selection": "the model returned invalid selection JSON",
}


def unanswered_report():
    entries = storage.list_unanswered(10000)
    print("=" * 60)
    print("UNANSWERED QUESTIONS")
    print("=" * 60)
    if not entries:
        print("None logged yet.\n")
        return
    counts = Counter(entry["question"].casefold().strip() for entry in entries)
    examples = {}
    for entry in entries:
        examples.setdefault(entry["question"].casefold().strip(), entry["question"])
    print(f"{len(entries)} unanswered questions ({len(counts)} different)\n")
    for key, count in counts.most_common(20):
        print(f"  {count:>4}  {examples[key]}")
    print("\nBy reason:")
    for reason, count in Counter(entry["reason"] for entry in entries).most_common():
        print(f"  {count:>4}  {REASONS.get(reason, reason)}")
    print()


def feedback_report():
    entries = storage.list_feedback(10000)
    print("=" * 60)
    print("ANSWER FEEDBACK")
    print("=" * 60)
    if not entries:
        print("None logged yet.\n")
        return
    latest = {}
    for entry in reversed(entries):
        latest[entry["answer_id"]] = entry
    votes = list(latest.values())
    ups = sum(entry["rating"] == "up" for entry in votes)
    downs = len(votes) - ups
    print(f"{len(votes)} rated answers: {ups} helpful, {downs} not helpful ")
    print(f"Helpful rate: {100 * ups / len(votes):.0f}%\n")
    for entry in [item for item in votes if item["rating"] == "down"][:10]:
        print(f"  Q: {entry['question']}")
        print(f"  A: {entry['answer'][:150].replace(chr(10), ' ')}")
    print()


def main():
    storage.initialize_database()
    unanswered_report()
    feedback_report()


if __name__ == "__main__":
    main()
