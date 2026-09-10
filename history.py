"""
Einfache JSON-Datei als Verlauf bereits geposteter Artikel, um Duplikate zu
vermeiden. Für ein Einzelnutzer-/Kleinteam-Tool reicht das völlig; bei Bedarf
lässt sich das leicht gegen eine echte Datenbank (z. B. SQLite) austauschen,
ohne dass sich an der Nutzung der Funktionen hier etwas ändern müsste.
"""

import json
import os
import re
import threading
from datetime import datetime, timezone

_lock = threading.Lock()


def _history_file() -> str:
    # Frisch gelesen (statt als Konstante beim Import fixiert), damit eine
    # Änderung über die Einstellungen-Seite ohne Neustart wirkt.
    return os.getenv("HISTORY_FILE", "posted_history.json")


def _normalize_title(title: str) -> str:
    t = (title or "").lower().strip()
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t)
    return t


def _load() -> list[dict]:
    if not os.path.exists(_history_file()):
        return []
    try:
        with open(_history_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    tmp_path = _history_file() + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, _history_file())


def find_posted(title: str, link: str) -> dict | None:
    """Gibt den Verlaufseintrag zurück, falls Titel (normalisiert) oder Link
    schon einmal gepostet bzw. versucht wurden - sonst None."""
    norm_title = _normalize_title(title)
    with _lock:
        for entry in _load():
            if entry.get("norm_title") == norm_title or (link and entry.get("link") == link):
                return entry
    return None


def record_posted(title: str, link: str, wp_id: int | None, wp_link: str = "") -> None:
    entry = {
        "title": title,
        "norm_title": _normalize_title(title),
        "link": link,
        "wp_id": wp_id,
        "wp_link": wp_link,
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        entries = _load()
        entries.append(entry)
        _save(entries)


def recent(limit: int = 20) -> list[dict]:
    with _lock:
        entries = _load()
    return list(reversed(entries))[:limit]
