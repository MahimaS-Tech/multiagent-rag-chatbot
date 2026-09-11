import hashlib
import math
import re
from collections import Counter

STOP = frozenset("a an and are as at be by can do does for from how i in is it me of on or our that the their this to was we what when where which who will with you your".split())


def tokens(value):
    return [w for w in re.findall(r"[\w]+", value.lower()) if w not in STOP and len(w) > 1]


def normalized(value):
    return " ".join(value.split())


def chunks(value, size, overlap):
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("Invalid chunk dimensions")
    value = value.replace("\r\n", "\n").strip()
    result, start = [], 0
    while start < len(value):
        end = min(start + size, len(value))
        if end < len(value):
            boundary = max(value.rfind("\n", start + size // 2, end), value.rfind(" ", start + size // 2, end))
            if boundary > start + overlap:
                end = boundary
        part = value[start:end].strip()
        if part:
            result.append(part)
        if end == len(value):
            break
        start = max(start + 1, end - overlap)
    return result


def hash_embedding(value, dimensions=1536):
    """Deterministic bag-of-words embedding for smoke tests, not semantic AI."""
    vector = [0.0] * dimensions
    for word, count in Counter(tokens(value)).items():
        index = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big") % dimensions
        vector[index] += count
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        vector[0] = 1.0
        return vector
    return [v / norm for v in vector]


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError("Embedding dimension mismatch")
    denominator = math.sqrt(sum(v * v for v in a) * sum(v * v for v in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


def lexical_score(query, content):
    q, counts = set(tokens(query)), Counter(tokens(content))
    return sum(min(counts.get(t, 0), 3) for t in q) / (len(q) * (1 + len(counts) / 100)) if q else 0.0
