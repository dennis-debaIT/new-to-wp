"""
Automatisierter Modus für das News-zu-WordPress-Tool - läuft ohne
Weboberfläche, z. B. per Windows-Aufgabenplanung oder Linux-Cronjob/systemd-
Timer (siehe PROXMOX_SETUP.md).

Ablauf pro Lauf:
    1. Durchsucht die in NEWS_QUERIES (Einstellungen bzw. .env) konfigurierten
       Suchbegriffe der Reihe nach.
    2. Nimmt pro Suchbegriff den ersten Treffer, der laut Verlauf
       (posted_history.json) noch nicht gepostet wurde.
    3. Schreibt ihn per KI um (inkl. SEO-Feldern, Kategorie-/Tag-Vorschlägen),
       ergänzt die festen Standard-Kategorien/Tags aus CRON_DEFAULT_*,
       sucht ein Bild und postet alles automatisch.
    4. Wiederholt das, bis CRON_MAX_POSTS_PER_RUN erreicht ist oder keine
       neuen Artikel mehr gefunden werden.

Aufruf (im news-to-wp-Ordner, mit aktivierter venv):
    python cron_post.py

Alle Werte (Suchbegriffe, WordPress-Zugang, Groq/Pexels-Keys usw.) werden
aus der .env gelesen - am einfachsten über die Einstellungen-Seite der
Weboberfläche gepflegt (app.py), da beide dieselbe .env-Datei nutzen.
"""

import time

import history
from core import (
    cfg,
    create_wp_post,
    extract_article_text,
    find_image,
    get_or_create_term,
    logger,
    notify_telegram,
    rewrite_with_groq,
    search_google_news,
    upload_image_to_wp,
)


def pick_next_article():
    """Geht die konfigurierten Suchbegriffe durch und liefert den ersten noch
    nicht geposteten Treffer zurück (oder (None, None))."""
    queries = [q.strip() for q in cfg.NEWS_QUERIES.split(",") if q.strip()]
    for query in queries:
        try:
            candidates = search_google_news(query, limit=10)
        except Exception as exc:
            logger.error("Suche nach '%s' fehlgeschlagen: %s", query, exc)
            continue
        for article in candidates:
            if history.find_posted(article["title"], article["link"]) is None:
                return query, article
    return None, None


def process_article(query: str, article: dict) -> bool:
    """Schreibt einen einzelnen Artikel um und postet ihn. Gibt True zurück,
    wenn erfolgreich gepostet wurde."""
    logger.info("Verarbeite Artikel: %s (Suchbegriff: %s)", article["title"], query)
    try:
        source_text = extract_article_text(article["link"]) or article["summary"]
        rewritten = rewrite_with_groq(article["title"], source_text, article["source"])

        default_categories = [c.strip() for c in cfg.CRON_DEFAULT_CATEGORY_NAMES.split(",") if c.strip()]
        default_tags = [t.strip() for t in cfg.CRON_DEFAULT_TAG_NAMES.split(",") if t.strip()]

        category_names = list(default_categories)
        for name in rewritten.get("categories", []):
            if name.lower() not in [c.lower() for c in category_names]:
                category_names.append(name)

        tag_names = list(default_tags)
        for name in rewritten.get("tags", []):
            if name.lower() not in [t.lower() for t in tag_names]:
                tag_names.append(name)

        category_ids = [cid for cid in (get_or_create_term("categories", n) for n in category_names) if cid]
        tag_ids = [tid for tid in (get_or_create_term("tags", n) for n in tag_names) if tid]

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
            cfg.DEFAULT_POST_STATUS,
            category_ids=category_ids,
            tag_ids=tag_ids,
            excerpt=rewritten.get("excerpt", ""),
            slug=rewritten.get("slug", ""),
        )

        history.record_posted(article["title"], article["link"], post.get("id"), post.get("link", ""))
        logger.info(
            "Gepostet: %s (WP-ID %s, Status %s, Kategorien: %s, Tags: %s)",
            rewritten["title"], post.get("id"), cfg.DEFAULT_POST_STATUS, category_names, tag_names,
        )
        notify_telegram(
            f"📰 Automatisch gepostet ({cfg.DEFAULT_POST_STATUS}):\n{rewritten['title']}\n{post.get('link', '')}"
        )
        return True
    except Exception as exc:
        logger.error("Fehler beim automatischen Posten von '%s': %s", article["title"], exc)
        # Als "versucht" im Verlauf vermerken, damit derselbe fehlerhafte
        # Artikel nicht bei jedem Lauf erneut hängen bleibt.
        history.record_posted(article["title"], article["link"], None, "")
        notify_telegram(f"⚠️ Fehler beim automatischen Posten von '{article['title']}': {exc}")
        return False


def run_once() -> None:
    if not cfg.NEWS_QUERIES.strip():
        logger.warning("NEWS_QUERIES ist leer - Cronjob hat nichts zu tun. Bitte in den Einstellungen setzen.")
        return

    posted_count = 0
    while posted_count < cfg.CRON_MAX_POSTS_PER_RUN:
        query, article = pick_next_article()
        if not article:
            logger.info("Keine neuen (noch nicht geposteten) Artikel gefunden - Lauf beendet.")
            break
        if process_article(query, article):
            posted_count += 1

    logger.info("Cronjob-Lauf beendet. %d Artikel gepostet.", posted_count)


if __name__ == "__main__":
    run_once()
