"""
Gemeinsame Kernlogik für das News-zu-WordPress-Tool: News suchen, Artikel per
KI (Groq) umschreiben (inkl. SEO-Feldern und Kategorie-/Tag-Vorschlägen),
Bild suchen (Pexels) und alles per WordPress-REST-API veröffentlichen.

Enthält außerdem die komplette Konfigurationsverwaltung (Config-Klasse +
Settings-Schema), die von der Einstellungen-Seite in der Weboberfläche
genutzt wird, um die .env-Datei zu lesen/schreiben und die laufende
Anwendung ohne Neustart zu aktualisieren.

Wird sowohl von der Weboberfläche (app.py) als auch vom Cronjob-Skript
(cron_post.py) verwendet, damit beide exakt dieselbe Logik nutzen.
"""

import json
import logging
import os
from urllib.parse import quote

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv, dotenv_values
from googlenewsdecoder import gnewsdecoder

ENV_PATH = os.getenv("ENV_FILE", ".env")

# ---------------------------------------------------------------------------
# Logging - Datei + Konsole, damit auch der Cronjob (ohne sichtbares Terminal)
# nachvollziehbare Protokolle hinterlässt.
# ---------------------------------------------------------------------------
logger = logging.getLogger("news_to_wp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    _file_handler = logging.FileHandler(os.getenv("LOG_FILE", "app.log"), encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

    _stream_handler = logging.StreamHandler()
    _stream_handler.setFormatter(_formatter)
    logger.addHandler(_stream_handler)


# ---------------------------------------------------------------------------
# Konfiguration - lebt in der .env-Datei, wird als Objekt `cfg` gehalten und
# kann per cfg.reload() zur Laufzeit neu eingelesen werden (z. B. nachdem die
# Einstellungen-Seite die .env aktualisiert hat) - ohne dass der Server neu
# gestartet werden muss.
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.reload()

    def reload(self) -> None:
        load_dotenv(ENV_PATH, override=True)
        self.WP_URL = os.getenv("WP_URL", "").rstrip("/")
        self.WP_USER = os.getenv("WP_USER", "")
        self.WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

        self.GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
        self.GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        try:
            self.GROQ_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.5"))
        except ValueError:
            self.GROQ_TEMPERATURE = 0.5

        self.PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
        self.PEXELS_ORIENTATION = os.getenv("PEXELS_ORIENTATION", "landscape")

        self.DEFAULT_POST_STATUS = os.getenv("DEFAULT_POST_STATUS", "draft")
        self.NEWS_LANG = os.getenv("NEWS_LANG", "de")
        self.NEWS_COUNTRY = os.getenv("NEWS_COUNTRY", "DE")
        try:
            self.SEARCH_RESULT_LIMIT = int(os.getenv("SEARCH_RESULT_LIMIT", "15"))
        except ValueError:
            self.SEARCH_RESULT_LIMIT = 15

        self.ARTICLE_TONE = os.getenv("ARTICLE_TONE", "sachlich")
        self.ARTICLE_LENGTH = os.getenv("ARTICLE_LENGTH", "mittel")

        self.HISTORY_FILE = os.getenv("HISTORY_FILE", "posted_history.json")
        self.LOG_FILE = os.getenv("LOG_FILE", "app.log")

        self.BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER", "")
        self.BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "")

        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

        self.NEWS_QUERIES = os.getenv("NEWS_QUERIES", "")
        try:
            self.CRON_MAX_POSTS_PER_RUN = int(os.getenv("CRON_MAX_POSTS_PER_RUN", "1"))
        except ValueError:
            self.CRON_MAX_POSTS_PER_RUN = 1
        self.CRON_DEFAULT_CATEGORY_NAMES = os.getenv("CRON_DEFAULT_CATEGORY_NAMES", "")
        self.CRON_DEFAULT_TAG_NAMES = os.getenv("CRON_DEFAULT_TAG_NAMES", "")

    @property
    def wp_auth(self):
        return (self.WP_USER, self.WP_APP_PASSWORD) if self.WP_USER else None


cfg = Config()


# ---------------------------------------------------------------------------
# Einstellungen-Schema (für die Admin-/Einstellungen-Seite in app.py)
# ---------------------------------------------------------------------------
FALLBACK_GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "gemma2-9b-it",
]

SETTINGS_SCHEMA = [
    {"key": "WP_URL", "label": "WordPress-URL", "type": "text", "group": "WordPress",
     "help": "z. B. https://deine-domain.de (ohne abschließenden Slash)"},
    {"key": "WP_USER", "label": "Benutzername", "type": "text", "group": "WordPress",
     "help": "WordPress-Benutzername (nicht die E-Mail-Adresse)"},
    {"key": "WP_APP_PASSWORD", "label": "Application Password", "type": "password", "group": "WordPress",
     "help": "Profil → Anwendungspasswörter in WordPress erzeugen"},

    {"key": "GROQ_API_KEY", "label": "API-Key", "type": "password", "group": "KI (Groq)",
     "help": "console.groq.com → API Keys"},
    {"key": "GROQ_MODEL", "label": "Modell", "type": "select_dynamic", "group": "KI (Groq)",
     "help": "Wird live von Groq abgerufen, sobald ein API-Key gespeichert ist"},
    {"key": "GROQ_TEMPERATURE", "label": "Kreativität (Temperature)", "type": "select", "group": "KI (Groq)",
     "options": ["0.2", "0.3", "0.5", "0.7", "0.9"],
     "help": "Niedrig = nüchterner/vorhersehbarer, hoch = kreativer/variabler"},

    {"key": "PEXELS_API_KEY", "label": "API-Key", "type": "password", "group": "Bild (Pexels)",
     "help": "pexels.com/api - kostenloser Key, optional"},
    {"key": "PEXELS_ORIENTATION", "label": "Bildausrichtung", "type": "select", "group": "Bild (Pexels)",
     "options": ["landscape", "portrait", "square"],
     "help": "Ausrichtung der gesuchten Beitragsbilder"},

    {"key": "DEFAULT_POST_STATUS", "label": "Standard-Status", "type": "select", "group": "Verhalten",
     "options": ["draft", "publish"], "help": "Vorauswahl in der Artikel-Liste"},
    {"key": "NEWS_LANG", "label": "Sprache (News-Suche)", "type": "text", "group": "Verhalten", "help": "z. B. de, en"},
    {"key": "NEWS_COUNTRY", "label": "Land (News-Suche)", "type": "text", "group": "Verhalten", "help": "z. B. DE, US"},
    {"key": "SEARCH_RESULT_LIMIT", "label": "Max. Suchergebnisse", "type": "number", "group": "Verhalten",
     "help": "Wie viele Treffer pro Suche angezeigt werden"},
    {"key": "ARTICLE_TONE", "label": "Standard-Ton", "type": "select", "group": "Verhalten",
     "options": ["sachlich", "locker", "boulevard"], "help": "Vorauswahl in der Artikel-Liste"},
    {"key": "ARTICLE_LENGTH", "label": "Standard-Länge", "type": "select", "group": "Verhalten",
     "options": ["kurz", "mittel", "lang"], "help": "Vorauswahl in der Artikel-Liste"},

    {"key": "HISTORY_FILE", "label": "Verlaufs-Datei", "type": "text", "group": "Duplikate & Logging",
     "help": "Pfad zur Datei für den Duplikat-Schutz"},
    {"key": "LOG_FILE", "label": "Log-Datei", "type": "text", "group": "Duplikate & Logging",
     "help": "Änderung wird erst nach einem Neustart des Tools wirksam"},

    {"key": "BASIC_AUTH_USER", "label": "Benutzername", "type": "text", "group": "Login-Schutz",
     "help": "Beide Felder setzen, um einen Login vor das Tool zu schalten; leer lassen = kein Login"},
    {"key": "BASIC_AUTH_PASSWORD", "label": "Passwort", "type": "password", "group": "Login-Schutz",
     "help": "Wird sofort aktiv - beim nächsten Laden fragt der Browser danach"},

    {"key": "TELEGRAM_BOT_TOKEN", "label": "Bot-Token", "type": "password", "group": "Telegram-Benachrichtigung",
     "help": "Bot über @BotFather anlegen, optional"},
    {"key": "TELEGRAM_CHAT_ID", "label": "Chat-ID", "type": "text", "group": "Telegram-Benachrichtigung",
     "help": "z. B. über @userinfobot ermitteln"},

    {"key": "NEWS_QUERIES", "label": "Suchbegriffe", "type": "text", "group": "Cronjob-Automatik",
     "help": "Komma-getrennt, für den automatisierten Modus (cron_post.py)"},
    {"key": "CRON_MAX_POSTS_PER_RUN", "label": "Max. Posts pro Lauf", "type": "number", "group": "Cronjob-Automatik",
     "help": "Wie viele Artikel ein einzelner Cronjob-Lauf maximal postet"},
    {"key": "CRON_DEFAULT_CATEGORY_NAMES", "label": "Standard-Kategorien", "type": "text", "group": "Cronjob-Automatik",
     "help": "Werden jedem Cronjob-Post zusätzlich zugewiesen, Komma-getrennt"},
    {"key": "CRON_DEFAULT_TAG_NAMES", "label": "Standard-Tags", "type": "text", "group": "Cronjob-Automatik",
     "help": "Werden jedem Cronjob-Post zusätzlich zugewiesen, Komma-getrennt"},
]


def get_groq_models() -> list[str]:
    """Live von Groq abgerufene Liste verfügbarer Modelle; fällt bei fehlendem
    Key oder Netzwerkfehler auf eine kleine Beispiel-Liste zurück."""
    if not cfg.GROQ_API_KEY:
        return FALLBACK_GROQ_MODELS
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {cfg.GROQ_API_KEY}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return FALLBACK_GROQ_MODELS
        ids = [m["id"] for m in resp.json().get("data", []) if "whisper" not in m.get("id", "").lower()]
        return sorted(ids) if ids else FALLBACK_GROQ_MODELS
    except Exception as exc:
        logger.warning("Groq-Modelle konnten nicht geladen werden: %s", exc)
        return FALLBACK_GROQ_MODELS


def _read_env_raw() -> dict:
    if os.path.exists(ENV_PATH):
        return {k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None}
    return {}


def get_settings_for_form() -> list[dict]:
    """Liefert die Settings-Felder gruppiert, mit aktuellem Wert - Passwörter
    werden nie im Klartext zurückgegeben, nur ein 'is_set'-Hinweis.

    Wichtig: Die Werte kommen aus `cfg` (nicht direkt aus der .env-Datei),
    weil cfg bereits die eingebauten Standardwerte (z. B. Ton "sachlich",
    Temperature 0.5) anwendet. Würde hier stattdessen die rohe .env gelesen,
    stünden Felder ohne expliziten .env-Eintrag leer im Formular - und beim
    nächsten Speichern würde der bisher aktive Standardwert versehentlich
    durch einen leeren Wert überschrieben."""
    groups: dict[str, list[dict]] = {}
    for field in SETTINGS_SCHEMA:
        entry = dict(field)
        current = getattr(cfg, field["key"], "")
        current_str = "" if current is None else str(current)
        if field["type"] == "password":
            entry["value"] = ""
            entry["is_set"] = bool(current_str)
        elif field["type"] == "select_dynamic":
            options = get_groq_models()
            if current_str and current_str not in options:
                options = [current_str] + options
            entry["type"] = "select"
            entry["options"] = options
            entry["value"] = current_str
        else:
            entry["value"] = current_str
        groups.setdefault(field["group"], []).append(entry)
    return [{"name": name, "fields": fields} for name, fields in groups.items()]


def _escape_env_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def save_settings(form_values: dict) -> None:
    """Schreibt die übergebenen Werte in die .env-Datei (Passwortfelder
    bleiben unverändert, wenn leer gelassen) und lädt die Konfiguration der
    laufenden Anwendung sofort neu (kein Neustart nötig, außer bei LOG_FILE)."""
    raw = _read_env_raw()
    for field in SETTINGS_SCHEMA:
        key = field["key"]
        if key not in form_values:
            continue
        val = (form_values.get(key) or "").strip()
        if field["type"] == "password" and val == "":
            continue  # leer lassen = nicht ändern
        raw[key] = val

    lines = ["# Wird von der Einstellungen-Seite im Webinterface verwaltet.", ""]
    current_group = None
    for field in SETTINGS_SCHEMA:
        if field["group"] != current_group:
            current_group = field["group"]
            lines.append(f"# --- {current_group} ---")
        lines.append(f'{field["key"]}="{_escape_env_value(str(raw.get(field["key"], "")))}"')

    known_keys = {f["key"] for f in SETTINGS_SCHEMA}
    extra_keys = [k for k in raw.keys() if k not in known_keys]
    if extra_keys:
        lines.append("# --- Sonstiges ---")
        for k in extra_keys:
            lines.append(f'{k}="{_escape_env_value(str(raw[k]))}"')

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    cfg.reload()
    logger.info("Einstellungen gespeichert und neu geladen.")


def test_connections() -> dict[str, str]:
    """Prüft die aktuell GESPEICHERTEN Zugangsdaten (nicht ungespeicherte
    Formulareingaben) durch einen minimalen, echten API-Aufruf je Dienst."""
    results: dict[str, str] = {}

    if cfg.WP_URL and cfg.WP_USER:
        try:
            r = requests.get(f"{cfg.WP_URL}/wp-json/wp/v2/users/me", auth=cfg.wp_auth, timeout=10)
            if r.status_code == 200:
                results["WordPress"] = f"OK - angemeldet als „{r.json().get('name', '?')}“"
            else:
                results["WordPress"] = f"Fehler (HTTP {r.status_code})"
        except Exception as exc:
            results["WordPress"] = f"Fehler: {exc}"
    else:
        results["WordPress"] = "Nicht konfiguriert"

    if cfg.GROQ_API_KEY:
        try:
            # Bewusst ein echter Mini-Chat-Aufruf mit dem KONFIGURIERTEN Modell,
            # nicht nur die Modell-Liste - so faellt ein ungueltiges/veraltetes
            # Modell (HTTP 404) hier schon auf, statt erst beim echten Posten.
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {cfg.GROQ_API_KEY}"},
                json={
                    "model": cfg.GROQ_MODEL,
                    "messages": [{"role": "user", "content": "Antworte nur mit OK."}],
                    "max_tokens": 5,
                },
                timeout=15,
            )
            if r.status_code == 200:
                results["Groq"] = f"OK (Modell „{cfg.GROQ_MODEL}“ funktioniert)"
            elif r.status_code == 404:
                results["Groq"] = (
                    f"Fehler: Modell „{cfg.GROQ_MODEL}“ wurde bei Groq nicht gefunden. "
                    "Bitte oben ein anderes Modell aus dem Dropdown wählen und erneut speichern."
                )
            elif r.status_code == 401:
                results["Groq"] = "Fehler: API-Key ungültig (HTTP 401)"
            else:
                results["Groq"] = f"Fehler (HTTP {r.status_code}): {r.text[:200]}"
        except Exception as exc:
            results["Groq"] = f"Fehler: {exc}"
    else:
        results["Groq"] = "Nicht konfiguriert"

    if cfg.PEXELS_API_KEY:
        try:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": cfg.PEXELS_API_KEY},
                params={"query": "test", "per_page": 1},
                timeout=10,
            )
            results["Pexels"] = "OK" if r.status_code == 200 else f"Fehler (HTTP {r.status_code})"
        except Exception as exc:
            results["Pexels"] = f"Fehler: {exc}"
    else:
        results["Pexels"] = "Nicht konfiguriert (optional)"

    if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID:
        try:
            r = requests.get(f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/getMe", timeout=10)
            results["Telegram"] = "OK" if r.status_code == 200 else f"Fehler (HTTP {r.status_code})"
        except Exception as exc:
            results["Telegram"] = f"Fehler: {exc}"
    else:
        results["Telegram"] = "Nicht konfiguriert (optional)"

    return results


# ---------------------------------------------------------------------------
# Telegram-Benachrichtigung (optional)
# ---------------------------------------------------------------------------
def notify_telegram(text: str) -> None:
    """Schickt eine Telegram-Nachricht, falls TELEGRAM_BOT_TOKEN/CHAT_ID gesetzt
    sind. Fehler werden nur geloggt, nie hochgeworfen - eine fehlgeschlagene
    Benachrichtigung darf den eigentlichen Posting-Vorgang nicht stören."""
    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cfg.TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": False},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Telegram-Benachrichtigung fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------
# News suchen
# ---------------------------------------------------------------------------
def search_google_news(query: str, limit: int | None = None) -> list[dict]:
    limit = int(limit) if limit is not None else cfg.SEARCH_RESULT_LIMIT
    url = (
        f"https://news.google.com/rss/search?q={quote(query)}"
        f"&hl={cfg.NEWS_LANG}&gl={cfg.NEWS_COUNTRY}&ceid={cfg.NEWS_COUNTRY}:{cfg.NEWS_LANG}"
    )
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries[:limit]:
        source = ""
        if hasattr(entry, "source"):
            source = getattr(entry.source, "title", "")
        summary_html = getattr(entry, "summary", "")
        summary_text = BeautifulSoup(summary_html, "html.parser").get_text()
        results.append(
            {
                "title": entry.title,
                "link": entry.link,
                "published": getattr(entry, "published", ""),
                "source": source,
                "summary": summary_text,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Google-News-Redirect-Links auf die echte Verlags-URL auflösen
# ---------------------------------------------------------------------------
def resolve_real_url(link: str) -> str:
    """news.google.com/rss/articles/...-Links zeigen nicht direkt auf den
    Artikel, sondern auf eine von Google verschlüsselte Zwischen-ID. Beim
    direkten Abruf landet man (v. a. aus der EU) auf Googles Cookie-Consent-
    Seite statt beim echten Artikel. gnewsdecoder bildet den internen Aufruf
    nach, den news.google.com selbst macht, um die echte Ziel-URL aufzulösen.
    Nicht-Google-Links werden unverändert zurückgegeben; schlägt die
    Auflösung fehl, ebenfalls (extract_article_text bekommt dann bestenfalls
    die Google-Zwischenseite - siehe Log-Warnung dort)."""
    if "news.google.com" not in link:
        return link
    try:
        result = gnewsdecoder(link)
        if result and result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
        logger.warning("Google-News-Link konnte nicht aufgelöst werden (kein decoded_url): %s", link)
    except Exception as exc:
        logger.warning("Google-News-Link konnte nicht aufgelöst werden (%s): %s", link, exc)
    return link


# ---------------------------------------------------------------------------
# Volltext der Originalseite holen (best effort)
# ---------------------------------------------------------------------------
def extract_article_text(url: str, max_chars: int = 14000) -> str:
    url = resolve_real_url(url)
    try:
        resp = requests.get(
            url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        # Wichtig: nicht nur <p>-Absätze einsammeln, sondern auch
        # <li>-Listenpunkte (z. B. CVE-Listen, Aufzählungen betroffener
        # Produkte/Versionen) und <blockquote>-Zitate - sonst gehen genau die
        # Details verloren, die in Sicherheitsmeldungen oft als Liste stehen.
        # <li>-Punkte werden mit "- " markiert, damit die KI sie später als
        # zusammengehörige Einzel-Fakten erkennt statt als Fließtext.
        collected = []
        for el in soup.find_all(["p", "li", "blockquote"]):
            txt = el.get_text(" ", strip=True)
            if not txt:
                continue
            if el.name == "li":
                collected.append(f"- {txt}")
            elif len(txt) > 40:
                collected.append(txt)
        text = "\n".join(collected)
        text = text[:max_chars]
        if len(text) < 300:
            # Verdächtig wenig Text (z. B. Paywall-/Consent-Seite statt echtem
            # Artikel) - lieber im Log sichtbar machen, statt still weiterzumachen.
            logger.warning(
                "Nur %d Zeichen Volltext extrahiert (%s) - evtl. Paywall/Consent-Seite statt Artikeltext.",
                len(text), url,
            )
        else:
            logger.info("Volltext extrahiert: %d Zeichen (%s)", len(text), url)
        return text
    except Exception as exc:
        logger.warning("Volltext konnte nicht geladen werden (%s): %s", url, exc)
        return ""


# ---------------------------------------------------------------------------
# Artikel per Groq umschreiben (inkl. SEO-Feldern & Kategorie-/Tag-Vorschlägen)
# ---------------------------------------------------------------------------
TONE_INSTRUCTIONS = {
    "sachlich": "sachlicher, neutraler journalistischer Stil",
    "locker": "lockerer, unterhaltsamer Blog-Stil, geduzt",
    "boulevard": "reißerischer Boulevard-Stil mit zugespitzten Formulierungen",
}
LENGTH_INSTRUCTIONS = {
    "kurz": "2-3 kompakte Absätze (ca. 120-180 Wörter)",
    "mittel": "3-5 Absätze Fließtext (ca. 250-400 Wörter)",
    "lang": "6-8 Absätze mit mehr Hintergrund und Einordnung (ca. 500-700 Wörter)",
}


def rewrite_with_groq(
    title: str,
    source_text: str,
    source_name: str,
    tone: str | None = None,
    length: str | None = None,
    existing_categories: list[str] | None = None,
    existing_tags: list[str] | None = None,
) -> dict:
    if not cfg.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY fehlt - bitte in den Einstellungen eintragen")

    tone = tone or cfg.ARTICLE_TONE
    length = length or cfg.ARTICLE_LENGTH
    tone_instruction = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["sachlich"])
    length_instruction = LENGTH_INSTRUCTIONS.get(length, LENGTH_INSTRUCTIONS["mittel"])
    existing_categories = existing_categories or []
    existing_tags = existing_tags or []

    prompt = f"""Du bist Redakteur für einen deutschsprachigen News-Blog.
Schreibe auf Basis der folgenden Rohinformationen einen eigenständigen,
komplett neu formulierten Artikel. WICHTIG: "Neu formuliert" bezieht sich auf
Satzbau und Wortwahl, NICHT auf den Inhalt - alle konkreten Fakten aus dem
Rohtext (Produkt-/Modellnamen, Versionsnummern, CVE-/Kennungen, Zahlen,
Daten, betroffene Systeme, technische Details, Zitate von Sprechern) müssen
vollständig erhalten bleiben. Lieber einen etwas längeren Artikel schreiben,
als Fakten aus Platzgründen wegzulassen. Nur die konkreten Satzformulierungen
dürfen nicht 1:1 aus der Quelle übernommen werden.

Ursprünglicher Titel: {title}
Quelle: {source_name}
Rohtext / Zusammenfassung:
{source_text}

Stil: {tone_instruction}.
Angestrebter Umfang: {length_instruction} - Details aus dem Rohtext haben
aber Vorrang vor dieser Richtgröße; fasse nichts Wichtiges weg, um kürzer zu
bleiben.

Bereits vorhandene Kategorien auf dem Blog (wenn thematisch passend bitte
bevorzugt wiederverwenden, statt neue zu erfinden): {", ".join(existing_categories) or "(keine bekannt)"}
Bereits vorhandene Tags (wenn thematisch passend bitte bevorzugt
wiederverwenden): {", ".join(existing_tags) or "(keine bekannt)"}

Anforderungen:
- Eigener, prägnanter Titel (max. 70 Zeichen)
- Angestrebter Umfang: {length_instruction}, aber alle Fakten aus dem Rohtext
  müssen enthalten sein - im Zweifel Vorrang vor der Wortzahl
- WICHTIG: Enthält der Rohtext eine Liste einzelner technischer Fakten
  (z. B. mehrere CVE-Nummern mit CVSS-Wert und betroffenen Produkten/
  Versionen, mehrere Änderungen, mehrere betroffene Modelle o. ä.), gib
  JEDEN einzelnen Punkt vollständig wieder - fasse eine Liste NIEMALS zu
  einer bloßen Gesamtzahl zusammen (z. B. nicht nur "22 Schwachstellen",
  sondern jede einzelne mit ihren Detailangaben). Nutze dafür im "content"
  eine <ul><li>-Liste - das zählt nicht gegen den angestrebten Umfang oben.
- Letzter Satz: Quellenhinweis ("Quelle: {source_name}")
- Kurze Meta-Description für SEO (max. 155 Zeichen)
- URL-Slug in Kleinbuchstaben, mit Bindestrichen statt Leerzeichen, ohne
  Umlaute/Sonderzeichen
- Kurzer, beschreibender Alt-Text für das Beitragsbild (max. 120 Zeichen)
- 1-3 passende Kategorien
- 3-6 passende Tags
- Antworte NUR als JSON-Objekt mit genau diesen Feldern:
  "title" (String), "content" (HTML - <p>-Absätze für Fließtext; zusätzlich
  <ul>/<li> erlaubt und erwünscht, wenn der Rohtext eine Liste einzelner
  Fakten enthält, siehe oben; sonst keine weitere Formatierung),
  "excerpt" (String), "slug" (String), "image_alt" (String),
  "categories" (Liste von Strings), "tags" (Liste von Strings)."""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg.GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg.GROQ_TEMPERATURE,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    if resp.status_code == 404:
        raise RuntimeError(
            f"Groq-Modell „{cfg.GROQ_MODEL}“ wurde nicht gefunden (HTTP 404). "
            "Bitte in den Einstellungen unter „KI (Groq)“ ein anderes Modell aus dem "
            "Dropdown wählen, speichern und mit „Verbindungen testen“ prüfen."
        )
    if resp.status_code == 401:
        raise RuntimeError("Groq-API-Key ungültig (HTTP 401). Bitte in den Einstellungen prüfen.")
    resp.raise_for_status()
    content_str = resp.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content_str)
    return {
        "title": parsed.get("title", title),
        "content": parsed.get("content", ""),
        "excerpt": (parsed.get("excerpt") or "")[:160],
        "slug": parsed.get("slug", "") or "",
        "image_alt": (parsed.get("image_alt") or "")[:125],
        "categories": [c for c in (parsed.get("categories") or []) if isinstance(c, str) and c.strip()],
        "tags": [t for t in (parsed.get("tags") or []) if isinstance(t, str) and t.strip()],
    }


# ---------------------------------------------------------------------------
# Passendes freies Bild suchen (Pexels)
# ---------------------------------------------------------------------------
def find_image(query: str) -> dict | None:
    if not cfg.PEXELS_API_KEY:
        return None
    resp = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": cfg.PEXELS_API_KEY},
        params={"query": query, "per_page": 1, "orientation": cfg.PEXELS_ORIENTATION},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    photos = resp.json().get("photos", [])
    if not photos:
        return None
    photo = photos[0]
    return {
        "url": photo["src"]["large"],
        "photographer": photo["photographer"],
        "photographer_url": photo["photographer_url"],
    }


# ---------------------------------------------------------------------------
# WordPress: Kategorien & Tags lesen / anlegen
# ---------------------------------------------------------------------------
def get_wp_terms(taxonomy: str) -> list[dict]:
    """Liefert alle vorhandenen Begriffe einer Taxonomie ('categories' oder
    'tags'), alphabetisch sortiert, über die REST API (paginiert)."""
    terms: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{cfg.WP_URL}/wp-json/wp/v2/{taxonomy}",
            auth=cfg.wp_auth,
            params={"per_page": 100, "page": page, "orderby": "name", "order": "asc"},
            timeout=15,
        )
        if resp.status_code != 200:
            break
        batch = resp.json()
        if not batch:
            break
        terms.extend({"id": t["id"], "name": t["name"]} for t in batch)
        total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return terms


def safe_get_wp_terms(taxonomy: str) -> list[dict]:
    """Wie get_wp_terms, gibt aber bei Fehlern (WP nicht erreichbar, falsche
    Zugangsdaten etc.) einfach eine leere Liste zurück, statt den restlichen
    Ablauf zu blockieren."""
    try:
        return get_wp_terms(taxonomy)
    except Exception as exc:
        logger.warning("Kategorien/Tags (%s) konnten nicht geladen werden: %s", taxonomy, exc)
        return []


def get_or_create_term(taxonomy: str, name: str) -> int | None:
    """Sucht einen Begriff (Kategorie/Tag) anhand des Namens; legt ihn an,
    falls er noch nicht existiert. Gibt die WordPress-Term-ID zurück."""
    name = (name or "").strip()
    if not name:
        return None

    try:
        resp = requests.get(
            f"{cfg.WP_URL}/wp-json/wp/v2/{taxonomy}",
            auth=cfg.wp_auth,
            params={"search": name, "per_page": 100},
            timeout=15,
        )
        if resp.status_code == 200:
            for t in resp.json():
                if t["name"].strip().lower() == name.lower():
                    return t["id"]

        resp = requests.post(
            f"{cfg.WP_URL}/wp-json/wp/v2/{taxonomy}",
            auth=cfg.wp_auth,
            json={"name": name},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("Neue/r %s angelegt: %s", taxonomy[:-1], name)
            return resp.json()["id"]
        logger.warning("Konnte %s '%s' nicht anlegen (HTTP %s): %s", taxonomy, name, resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.warning("Fehler beim Suchen/Anlegen von %s '%s': %s", taxonomy, name, exc)
    return None


# ---------------------------------------------------------------------------
# WordPress: Bild hochladen + Beitrag anlegen
# ---------------------------------------------------------------------------
def upload_image_to_wp(image_url: str, filename: str, alt_text: str = "") -> int | None:
    img_resp = requests.get(image_url, timeout=20)
    img_resp.raise_for_status()
    media_resp = requests.post(
        f"{cfg.WP_URL}/wp-json/wp/v2/media",
        auth=cfg.wp_auth,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/jpeg",
        },
        data=img_resp.content,
        timeout=30,
    )
    media_resp.raise_for_status()
    media_id = media_resp.json()["id"]

    if alt_text:
        try:
            requests.post(
                f"{cfg.WP_URL}/wp-json/wp/v2/media/{media_id}",
                auth=cfg.wp_auth,
                json={"alt_text": alt_text},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Alt-Text für Bild %s konnte nicht gesetzt werden: %s", media_id, exc)

    return media_id


def create_wp_post(
    title: str,
    content_html: str,
    media_id: int | None,
    status: str,
    category_ids: list[int] | None = None,
    tag_ids: list[int] | None = None,
    excerpt: str = "",
    slug: str = "",
) -> dict:
    payload: dict = {"title": title, "content": content_html, "status": status}
    if media_id:
        payload["featured_media"] = media_id
    if category_ids:
        payload["categories"] = category_ids
    if tag_ids:
        payload["tags"] = tag_ids
    if excerpt:
        payload["excerpt"] = excerpt
    if slug:
        payload["slug"] = slug
    resp = requests.post(
        f"{cfg.WP_URL}/wp-json/wp/v2/posts",
        auth=cfg.wp_auth,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
