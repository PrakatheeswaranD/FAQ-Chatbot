import math
import re

TOP_K = 8
QUESTION_WEIGHT = 2.0

STOPWORDS = {
    "a", "an", "the", "is", "are", "am", "was", "were", "be", "been",
    "do", "does", "did", "can", "could", "will", "would", "shall", "should",
    "i", "me", "my", "we", "our", "you", "your", "it", "its", "there",
    "what", "which", "who", "whom", "when", "where", "how", "why",
    "of", "in", "on", "at", "to", "for", "from", "by", "with", "about",
    "and", "or", "if", "any", "this", "that", "these", "those", "as",
    "tell", "please", "get", "have", "has", "need", "much", "many",
    "kit", "college", "campus", "student", "students",
}

SYNONYMS = {
    "close": "hour", "closes": "hour", "open": "hour", "opens": "hour",
    "timing": "hour", "timings": "hour", "time": "hour", "hrs": "hour",
    "cost": "fee", "price": "fee", "charge": "fee", "amount": "fee",
    "girl": "female", "girls": "female", "women": "female", "ladies": "female",
    "boys": "male", "boy": "male",
    "head": "principal",
    "bus": "bus", "buses": "bus", "transport": "bus",
    "phone": "contact", "call": "contact", "number": "contact",
    "mail": "email", "gmail": "email",
    "job": "placement", "jobs": "placement", "recruit": "placement",
    "recruiters": "placement", "companies": "placement", "company": "placement",
    "exam": "exam", "exams": "exam", "examination": "exam",
    "ragged": "ragging", "rag": "ragging",
    "food": "mess", "breakfast": "mess", "lunch": "mess", "dinner": "mess",
    "branch": "course", "branches": "course", "department": "course",
    "departments": "course", "program": "course", "programme": "course",
    "result": "result", "marks": "result",
    "pay": "payment", "paid": "payment",
    "far": "distance", "km": "distance", "reach": "distance",
}


def _stem(word):
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) >= 6:
        return word[:-3]
    if word.endswith("s") and not word.endswith("ss") and len(word) >= 4:
        return word[:-1]
    return word


def tokenize(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    tokens = []
    for word in words:
        if word in STOPWORDS:
            continue
        tokens.append(_stem(SYNONYMS.get(word, word)))
    return tokens


class FaqRetriever:
    def __init__(self, faqs):
        self.faqs = faqs
        self.documents = []
        document_frequency = {}

        for faq in faqs:
            weights = {}
            for token in tokenize(faq["question"]):
                weights[token] = weights.get(token, 0) + QUESTION_WEIGHT
            for token in tokenize(faq["answer"]):
                weights[token] = weights.get(token, 0) + 1
            self.documents.append(weights)
            for token in weights:
                document_frequency[token] = document_frequency.get(token, 0) + 1

        total = len(faqs)
        self.idf = {
            token: math.log((total + 1) / (count + 0.5))
            for token, count in document_frequency.items()
        }

    def search(self, question, top_k=TOP_K):
        query = set(tokenize(question))
        scored = []

        for index, weights in enumerate(self.documents):
            score = sum(
                weights[token] * self.idf[token]
                for token in query
                if token in weights
            )
            if score > 0:
                scored.append((score, index))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [self.faqs[index] for _, index in scored[:top_k]]
