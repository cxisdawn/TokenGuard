"""Small real Python bug-fix fixture. Only arithmetic.py may be changed."""
def divide(a: float, b: float) -> float:
    return a // b  # BUG: true division required
