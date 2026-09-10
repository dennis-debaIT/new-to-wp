"""
News-zu-WordPress Tool - Weboberfläche
=======================================
Suchbegriff eingeben -> Google News RSS Ergebnisse anzeigen -> Artikel
auswählen -> Kategorien/Tags auswählen oder neu anlegen -> Ton & Länge
festlegen -> Artikel wird per Groq-KI komplett neu geschrieben (inkl.
SEO-Feldern und Kategorie-/Tag-Vorschlägen), ein passendes freies Bild
(Pexels) gesucht und alles als Entwurf (oder direkt veröffentlicht) per
WordPress REST API gepostet. Bereits geposteten Artikeln wird ein Hinweis
angezeigt, um Duplikate zu vermeiden.

Über den Button "⚙️ Einstellungen" (Popup) lassen sich alle API-Keys und
sonstigen Optionen direkt im Browser eintragen - keine manuelle .env-
Bearbeitung mehr nötig. Änderungen werden sofort übernommen (Ausnahme:
Log-Datei-Pfad, der erst nach einem Neustart wirkt).

Start:
    uvicorn app:app --host 0.0.0.0 --port 8000

Für den automatisierten Betrieb ohne Weboberfläche siehe cron_post.py.
"""

import secrets
import time

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import core
import history
from core import (
    LENGTH_INSTRUCTIONS,
    TONE_INSTRUCTIONS,
    cfg,
    create_wp_post,
    extract_article_text,
    find_image,
    get_or_create_term,
    get_settings_for_form,
    logger,
    notify_telegram,
    rewrite_with_groq,
    safe_get_wp_terms,
    save_settings,
    search_google_news,
    test_connections,
    upload_image_to_wp,
)

security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    """Optionaler HTTP-Basic-Login-Schutz. Greift nur, wenn in den
    Einstellungen ein Benutzername gesetzt ist - ansonsten bleibt das Tool
    wie bisher offen (weiterhin nur intern/hinter VPN oder Reverse-Proxy-Auth
    betreiben!). Liest cfg live, damit eine Änderung sofort wirkt."""
    if not cfg.BASIC_AUTH_USER:
        return
    valid = credentials is not None and secrets.compare_digest(
        credentials.username, cfg.BASIC_AUTH_USER
    ) and secrets.compare_digest(credentials.password, cfg.BASIC_AUTH_PASSWORD)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nicht autorisiert",
            headers={"WWW-Authenticate": "Basic"},
        )


app = FastAPI(title="News-zu-WordPress Tool", dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Einfacher In-Memory-Cache für die letzte Suche (reicht für Einzelnutzer-Tool).
# Für Mehrbenutzer-Betrieb müsste man das pro Session/DB auslagern.
SEARCH_CACHE: dict[str, dict] = {}

TONE_OPTIONS = list(TONE_INSTRUCTIONS.keys())
LENGTH_OPTIONS = list(LENGTH_INSTRUCTIONS.keys())


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "results": None,
        "query": "",
        "status_default": cfg.DEFAULT_POST_STATUS,
        "tone_options": TONE_OPTIONS,
        "length_options": LENGTH_OPTIONS,
        "default_tone": cfg.ARTICLE_TONE,
        "default_length": cfg.ARTICLE_LENGTH,
        "settings_groups": get_settings_for_form(),
        "settings_open": False,
        "settings_success": None,
        "settings_error": None,
        "test_results": None,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", _base_context(request))


@app.post("/search", response_class=HTMLResponse)
def search(request: Request, query: str = Form(...)):
    logger.info("Suche gestartet: %s", query)
    results = search_google_news(query)
    SEARCH_CACHE.clear()
    for i, r in enumerate(results):
        SEARCH_CACHE[str(i)] = r
        posted = history.find_posted(r["title"], r["link"])
        r["already_posted"] = posted is not None
        r["posted_at"] = posted.get("posted_at") if posted else None

    categories = safe_get_wp_terms("categories")
    tags = safe_get_wp_terms("tags")

    ctx = _base_context(request)
    ctx.update(
        {
            "results": results,
            "query": query,
            "categories": categories,
            "tags": tags,
        }
    )
    return templates.TemplateResponse("index.html", ctx)


@app.post("/publish", response_class=HTMLResponse)
def publish(
    request: Request,
    article_index: str = Form(...),
    post_status: str = Form(""),
    category_ids: list[str] = Form([]),
    new_categories: str = Form(""),
    tag_ids: list[str] = Form([]),
    new_tags: str = Form(""),
    tone: str = Form(""),
    length: str = Form(""),
    force_repost: str = Form(""),
):
    post_status = post_status or cfg.DEFAULT_POST_STATUS
    tone = tone or cfg.ARTICLE_TONE
    length = length or cfg.ARTICLE_LENGTH

    article = SEARCH_CACHE.get(article_index)
    if not article:
        ctx = _base_context(request)
        ctx["error"] = "Artikel nicht mehr im Cache (Server evtl. neu gestartet) – bitte neu suchen."
        return templates.TemplateResponse("index.html", ctx)

    # Duplikat-Schutz: außer der Nutzer bestätigt ausdrücklich "trotzdem posten".
    existing = history.find_posted(article["title"], article["link"])
    if existing and force_repost != "1":
        logger.info("Posten abgebrochen (bereits gepostet): %s", article["title"])
        ctx = _base_context(request)
        ctx["error"] = (
            f"Dieser Artikel wurde bereits am {existing.get('posted_at', '?')} gepostet "
            f"(WordPress-ID {existing.get('wp_id', '?')}). Aktiviere unten „trotzdem posten“, "
            "falls du ihn wirklich erneut veröffentlichen willst."
        )
        return templates.TemplateResponse("index.html", ctx)

    try:
        existing_categories_map = {c["id"]: c["name"] for c in safe_get_wp_terms("categories")}
        existing_tags_map = {t["id"]: t["name"] for t in safe_get_wp_terms("tags")}

        category_id_ints: list[int] = []
        category_names: list[str] = []
        for c in category_ids:
            if c.strip().isdigit():
                cid = int(c)
                category_id_ints.append(cid)
                category_names.append(existing_categories_map.get(cid, f"#{cid}"))

        tag_id_ints: list[int] = []
        tag_names: list[str] = []
        for t in tag_ids:
            if t.strip().isdigit():
                tid = int(t)
                tag_id_ints.append(tid)
                tag_names.append(existing_tags_map.get(tid, f"#{tid}"))

        for name in [n.strip() for n in new_categories.split(",") if n.strip()]:
            if name.lower() in [c.lower() for c in category_names]:
                continue
            cid = get_or_create_term("categories", name)
            if cid:
                category_id_ints.append(cid)
                category_names.append(name)

        for name in [n.strip() for n in new_tags.split(",") if n.strip()]:
            if name.lower() in [t.lower() for t in tag_names]:
                continue
            tid = get_or_create_term("tags", name)
            if tid:
                tag_id_ints.append(tid)
                tag_names.append(name)

        source_text = extract_article_text(article["link"]) or article["summary"]
        rewritten = rewrite_with_groq(
            article["title"],
            source_text,
            article["source"],
            tone=tone,
            length=length,
            existing_categories=list(existing_categories_map.values()),
            existing_tags=list(existing_tags_map.values()),
        )

        # KI-Vorschläge für Kategorien/Tags ergänzen (bereits vorhandene/gewählte
        # Namen werden nicht doppelt angelegt).
        ai_categories_used, ai_tags_used = [], []
        for name in rewritten.get("categories", []):
            if name.lower() in [c.lower() for c in category_names]:
                continue
            cid = get_or_create_term("categories", name)
            if cid:
                category_id_ints.append(cid)
                category_names.append(name)
                ai_categories_used.append(name)

        for name in rewritten.get("tags", []):
            if name.lower() in [t.lower() for t in tag_names]:
                continue
            tid = get_or_create_term("tags", name)
            if tid:
                tag_id_ints.append(tid)
                tag_names.append(name)
                ai_tags_used.append(name)

        image = find_image(rewritten["title"] or article["title"])
        media_id = None
        if image:
            media_id = upload_image_to_wp(
                image["url"],
                f"news-{int(time.time())}.jpg",
                alt_text=rewritten.get("image_alt", ""),
            )

        post = create_wp_post(
            rewritten["title"],
            rewritten["content"],
            media_id,
            post_status,
            category_ids=category_id_ints,
            tag_ids=tag_id_ints,
            excerpt=rewritten.get("excerpt", ""),
            slug=rewritten.get("slug", ""),
        )

        history.record_posted(article["title"], article["link"], post.get("id"), post.get("link", ""))
        logger.info("Erfolgreich gepostet: %s (WP-ID %s, Status %s)", rewritten["title"], post.get("id"), post_status)
        notify_telegram(
            f"📰 Neuer Artikel gepostet ({post_status}):\n{rewritten['title']}\n{post.get('link', '')}"
        )

        ctx = _base_context(request)
        ctx["success"] = {
            "title": rewritten["title"],
            "status": post_status,
            "edit_link": post.get("link", ""),
            "wp_id": post.get("id"),
            "image_credit": image,
            "excerpt": rewritten.get("excerpt", ""),
            "slug": rewritten.get("slug", ""),
            "categories": category_names,
            "tags": tag_names,
            "ai_categories_used": ai_categories_used,
            "ai_tags_used": ai_tags_used,
            "tone": tone,
            "length": length,
        }
        return templates.TemplateResponse("index.html", ctx)
    except Exception as exc:  # bewusst breit, damit der Nutzer eine Fehlermeldung sieht
        logger.error("Fehler beim Posten von '%s': %s", article.get("title"), exc)
        notify_telegram(f"⚠️ Fehler beim Posten von '{article.get('title')}': {exc}")
        ctx = _base_context(request)
        ctx["error"] = f"Fehler beim Verarbeiten: {exc}"
        return templates.TemplateResponse("index.html", ctx)


@app.post("/settings", response_class=HTMLResponse)
async def update_settings(request: Request):
    form = await request.form()
    values = {field["key"]: form.get(field["key"], "") for field in core.SETTINGS_SCHEMA}

    ctx = _base_context(request)
    ctx["settings_open"] = True
    try:
        save_settings(values)
        ctx["settings_groups"] = get_settings_for_form()  # frisch nach dem Speichern
        ctx["settings_success"] = "Einstellungen gespeichert und übernommen."
    except Exception as exc:
        logger.error("Einstellungen konnten nicht gespeichert werden: %s", exc)
        ctx["settings_error"] = f"Fehler beim Speichern: {exc}"
    return templates.TemplateResponse("index.html", ctx)


@app.post("/settings/test", response_class=HTMLResponse)
def run_connection_test(request: Request):
    ctx = _base_context(request)
    ctx["settings_open"] = True
    ctx["test_results"] = test_connections()
    return templates.TemplateResponse("index.html", ctx)
