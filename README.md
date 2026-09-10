# News → WordPress Tool

Web-Interface und Cronjob-Modus, um News automatisiert (per KI umgeschrieben,
inkl. SEO-Feldern, passendem Bild, Kategorien und Tags) in ein
selbstgehostetes WordPress zu posten — mit Duplikat-Schutz, optionalem
Login, optionaler Telegram-Benachrichtigung und einer Einstellungen-Seite
im Browser für alle API-Keys und Optionen (keine manuelle `.env`-Bearbeitung
mehr nötig).

**Läuft das Tool in einem Proxmox-Container (Ubuntu 24.04)?** Dann direkt zu
[`PROXMOX_SETUP.md`](./PROXMOX_SETUP.md) springen - dort steht die komplette
Schritt-für-Schritt-Einrichtung inkl. systemd-Diensten und Reverse Proxy.

## Aufbau

- `core.py` — gesamte Kernlogik (News-Suche, KI-Umschreiben, Bild, WordPress-API, Konfiguration, Logging, Telegram). Wird von Web-App und Cronjob gemeinsam genutzt.
- `history.py` — Verlauf bereits geposteter Artikel (Duplikat-Schutz), als JSON-Datei.
- `app.py` — Weboberfläche (FastAPI) inkl. Einstellungen-Popup, für die manuelle Nutzung.
- `cron_post.py` — Automatisierter Modus ohne Weboberfläche, für Cronjob/systemd-Timer.
- `.env` — wird vom Tool selbst geschrieben, sobald du etwas in den Einstellungen speicherst. `env.example.txt` dient nur noch als Referenz.

## 1. Voraussetzungen

- Python 3.10+
- Ein selbstgehostetes WordPress mit aktivierter REST API (Standard seit WP 5.6)
- Ein **Application Password** in WordPress:
  Profil → Anwendungspasswörter → Namen vergeben (z. B. "News-Bot") → erzeugen.
  Das Passwort wird nur einmal angezeigt.
- Ein **Groq API-Key**: https://console.groq.com → API Keys
- Ein **Pexels API-Key** (kostenlos, optional): https://www.pexels.com/api/
- Optional für Benachrichtigungen: ein **Telegram-Bot** (über @BotFather
  anlegen) und deine Chat-ID (z. B. über @userinfobot ermitteln)

Alle diese Werte trägst du **nach dem Start** direkt im Browser ein (siehe
Schritt 3) - keine manuelle `.env`-Datei nötig.

## 2. Installation

```bash
cd news-to-wp
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

(Für die Installation in einem Proxmox-Ubuntu-24.04-Container: siehe
[`PROXMOX_SETUP.md`](./PROXMOX_SETUP.md).)

## 3. Weboberfläche starten & einrichten

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Im Browser: `http://SERVER-IP:8000` öffnen. Oben rechts auf **"⚙️
Einstellungen"** klicken — dort trägst du in einem Popup ein:

- **WordPress**: URL, Benutzername, Application Password
- **KI (Groq)**: API-Key, Modell (wird live von Groq abgerufen, sobald der
  Key gespeichert ist — Auswahl per Dropdown), Kreativität/Temperature
- **Bild (Pexels)**: API-Key, bevorzugte Bildausrichtung
- **Verhalten**: Standard-Post-Status, Sprache/Land der News-Suche, Anzahl
  Suchergebnisse, Standard-Ton und -Länge für Artikel
- **Duplikate & Logging**: Pfade für Verlaufs- und Log-Datei
- **Login-Schutz**: optionaler Benutzername/Passwort fürs Webinterface
- **Telegram-Benachrichtigung**: optionaler Bot-Token/Chat-ID
- **Cronjob-Automatik**: Suchbegriffe, max. Posts pro Lauf, Standard-
  Kategorien/Tags für den automatisierten Modus (siehe Schritt 5)

Passwörter/Keys werden dabei **nie im Klartext angezeigt** — ein leeres
Feld beim Speichern bedeutet "unverändert lassen". Mit **"🔌 Verbindungen
testen"** direkt im Popup prüfen, ob WordPress/Groq/Pexels/Telegram mit den
gespeicherten Zugangsdaten erreichbar sind. Änderungen wirken sofort, ohne
Neustart (einzige Ausnahme: der Pfad der Log-Datei).

Für den Dauerbetrieb empfiehlt sich ein systemd-Service (Beispiel unten oder
ausführlicher in `PROXMOX_SETUP.md`), plus ein Eintrag in deinem HAProxy (du
hast ja schon OPNsense/HAProxy im Einsatz), damit das Tool z. B. unter
`news.deine-domain.de` erreichbar ist — inklusive TLS.

Minimalbeispiel für einen systemd-Service (`/etc/systemd/system/news-to-wp.service`):

```ini
[Unit]
Description=News to WordPress Tool
After=network.target

[Service]
WorkingDirectory=/pfad/zu/news-to-wp
ExecStart=/pfad/zu/news-to-wp/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
EnvironmentFile=/pfad/zu/news-to-wp/.env

[Install]
WantedBy=multi-user.target
```

## 4. Bedienung (Weboberfläche)

1. Suchbegriff eingeben (z. B. "Smart Home") → Suchen
2. Passenden Artikel per Radiobutton auswählen. Bereits früher geposteten
   Artikeln wird ein gelbes „Bereits gepostet“-Badge angezeigt — beim Posten
   wird das zusätzlich serverseitig geprüft und blockiert, außer du aktivierst
   „trotzdem posten“.
3. Bei **Kategorien** und **Tags** die passenden vorhandenen Einträge per
   Checkbox anhaken (werden live aus WordPress ausgelesen). Fehlt ein
   Begriff, einfach ins Textfeld darunter eintragen — mehrere durch Komma
   getrennt. Die KI schlägt beim Posten zusätzlich automatisch 1-3
   Kategorien und 3-6 Tags vor; vorhandene Begriffe werden dabei
   wiederverwendet statt dupliziert.
4. **Ton** (sachlich/locker/boulevard) und **Länge** (kurz/mittel/lang) für
   den generierten Artikel wählen.
5. "Als Entwurf speichern" oder "Direkt veröffentlichen" wählen
6. Auf "Ausgewählten Artikel umschreiben & posten" klicken
7. Das Tool holt den Volltext der Originalseite (best effort), lässt Groq
   einen komplett neuen Artikel + Titel + Meta-Description + Slug +
   Bild-Alt-Text formulieren, sucht bei Pexels ein passendes Bild, lädt es
   mit Alt-Text in die WordPress-Mediathek hoch und legt den Beitrag mit
   Beitragsbild, Kategorien, Tags, Excerpt und Slug an. Der Artikel wird im
   Verlauf vermerkt, damit er nicht versehentlich doppelt gepostet wird.

## 5. Automatisierter Betrieb ohne Weboberfläche (`cron_post.py`)

In den Einstellungen (Gruppe "Cronjob-Automatik") konfigurieren:

- **Suchbegriffe** — Komma-getrennt, werden der Reihe nach abgearbeitet
  (z. B. `Smart Home,Photovoltaik,KI`)
- **Max. Posts pro Lauf** — wie viele Artikel pro Lauf maximal gepostet
  werden (Standard: 1)
- **Standard-Kategorien** / **Standard-Tags** — feste Begriffe, die
  zusätzlich zu den KI-Vorschlägen jedem automatisch geposteten Artikel
  zugewiesen werden

Aufruf: `python cron_post.py` (aus dem `news-to-wp`-Ordner, mit aktivierter
venv). Pro Suchbegriff wird der erste noch nicht geposteten Treffer
verwendet (Duplikat-Check über denselben Verlauf wie die Weboberfläche).

**Auf Ubuntu/Proxmox**: siehe `PROXMOX_SETUP.md`, Abschnitt 7, für einen
systemd-Timer (empfohlen statt klassischem Cron).

**Windows-Aufgabenplanung** (Task Scheduler):
- Aktion: Programm starten
- Programm/Skript: `C:\t\news-to-wp\venv\Scripts\python.exe`
- Argumente: `cron_post.py`
- Starten in: `C:\t\news-to-wp`
- Trigger: z. B. täglich alle 4 Stunden

Alle Läufe (Web und Cron) schreiben nach `app.log` (Pfad in den
Einstellungen konfigurierbar) sowie auf die Konsole.

## 6. Login-Schutz fürs Webinterface (optional)

Standardmäßig ist die Weboberfläche **ohne** Login erreichbar (nur intern/
hinter VPN oder Basic-Auth im Reverse Proxy betreiben!). Um einen einfachen
HTTP-Basic-Login zu aktivieren: Einstellungen öffnen → Gruppe "Login-Schutz"
→ Benutzername und Passwort eintragen → speichern. Wirkt sofort, beim
nächsten Laden fragt der Browser danach.

## 7. Telegram-Benachrichtigung (optional)

Bot über [@BotFather](https://t.me/BotFather) anlegen (`/newbot`), Token in
den Einstellungen unter "Telegram-Benachrichtigung" eintragen. Chat-ID z. B.
über [@userinfobot](https://t.me/userinfobot) ermitteln. Danach bekommst du
nach jedem (erfolgreichen oder fehlgeschlagenen) Postversuch eine Nachricht
— egal ob über die Weboberfläche oder `cron_post.py`.

## 8. Hinweise & Grenzen

- **Rechtliches**: Der Artikeltext wird bewusst komplett neu formuliert
  (nicht 1:1 übernommen). Trotzdem: bei heiklen Themen oder wörtlichen
  Zitaten aus der Quelle vor dem Veröffentlichen selbst gegenlesen —
  insbesondere im automatisierten Cronjob-Modus, wo niemand vor dem Posten
  gegenliest.
- Der In-Memory-Cache der Suchergebnisse (Weboberfläche) ist einfach
  gehalten (kein Mehrbenutzer-Betrieb, geht bei Neustart verloren) — für ein
  privates Redaktionstool aber ausreichend.
- Der Duplikat-Verlauf (`posted_history.json`) gleicht normalisierte Titel
  und Links ab. Bei einem manuellen Löschen der Datei "vergisst" das Tool
  alle bisherigen Posts.
- Wenn kein passendes Pexels-Bild gefunden wird, wird der Beitrag ohne
  Beitragsbild angelegt.
- Kategorien/Tags werden bei jeder Suche neu von WordPress abgerufen
  (kein Caching) — Änderungen in WordPress erscheinen also sofort in der
  Auswahl. Ist WordPress beim Abruf nicht erreichbar, wird die Auswahl
  einfach leer angezeigt (Suche/Posten funktioniert trotzdem weiter,
  nur ohne Kategorien/Tags).
- Die `.env`-Datei enthält nach dem ersten Speichern deine Zugangsdaten im
  Klartext auf dem Server — Dateiberechtigungen entsprechend setzen
  (z. B. nur lesbar für den Benutzer, unter dem das Tool läuft).

## 9. Nächste Ausbaustufen (optional)

- Mehrfachauswahl mehrerer Artikel auf einmal + Vorschau/Freigabe vor dem Posten
- Verlauf als kleine Oberfläche einsehbar machen (aktuell nur JSON-Datei)
- Mehrere Sprachen/Länder gleichzeitig durchsuchen
