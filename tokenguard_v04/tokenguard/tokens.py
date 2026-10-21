"""Explicit estimates. No dependency downloads and no inferred API billing."""
import math
import os


def estimate(text: str, model: str | None = None) -> dict:
    model = model or os.environ.get('TOKENGUARD_MODEL', 'deepseek-flash')
    # Deliberately labeled heuristic; different tokenizers/languages vary substantially.
    ascii_chars = sum(ord(c) < 128 for c in text)
    count = math.ceil(ascii_chars / 4 + (len(text)-ascii_chars))
    return {'model': model, 'method': 'ascii_chars/4 + non_ascii_chars; uncalibrated heuristic',
            'estimated_tokens': count, 'exact_model_tokenizer': False}
