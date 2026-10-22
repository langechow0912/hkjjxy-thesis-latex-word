"""Load machine-local credentials without exposing them to the browser."""
import os
from pathlib import Path


def parse_pat(value):
    """Accept a raw token or the PAT= assignment supplied in credential files."""
    value = value.lstrip('\ufeff').strip()
    if value.lower().startswith('pat='):
        value = value.split('=', 1)[1].strip()
    return value


def load_pat(root):
    if os.environ.get("DAYIPAPER_PAT", "").strip():
        return
    configured_path = os.environ.get("DAYIPAPER_PAT_FILE", "").strip()
    path = Path(configured_path) if configured_path else Path(root) / ".secrets" / "dayipaper.pat"
    if path.is_file():
        token = parse_pat(path.read_text(encoding="utf-8-sig"))
        if token:
            os.environ["DAYIPAPER_PAT"] = token
