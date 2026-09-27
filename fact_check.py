def _stored_answer(faq, language):
    if language == "ta" and faq.get("answer_ta"):
        return faq["answer_ta"].strip()
    return faq["answer"].strip()


def compose_faq_answer(faqs, language="en"):
    return "\n\n".join(_stored_answer(faq, language) for faq in faqs)


def is_faq_only_answer(answer, faqs, language="en"):
    return answer == compose_faq_answer(faqs, language=language)
