"""
Minimal, stdlib-only .env file loader -- no `python-dotenv` dependency, kept
consistent with the rest of the engine (dependencies = []).

Only supports simple `KEY=VALUE` lines (optionally quoted, `#` comments,
blank lines skipped). Never overrides a variable that's already set in the
real process environment, so real env vars / explicit CLI flags always take
priority over whatever is in the file.

This is for *scanner configuration* (e.g. your own LLM API key, whether to
enable enrichment) -- it has nothing to do with the target repo's own
.env/.env.example files, which the scanner separately reads (key NAMES
only, never values) as a static AI-stack signal. Don't confuse the two.
"""
import os


def load_env_file(path: str = ".env") -> bool:
    """Load KEY=VALUE pairs from `path` into os.environ (if the file
    exists). Returns True if the file was found and read, False otherwise.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
    return True


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish environment variable (true/1/yes/on, case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
