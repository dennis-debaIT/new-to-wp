# Installation in einem Proxmox-Container (Ubuntu 24.04)

Diese Anleitung geht davon aus, dass du das Tool in einem **LXC-Container**
unter Proxmox laufen lässt (leichtgewichtiger als eine volle VM, für ein
Python-Webtool wie dieses völlig ausreichend). Alternativ funktionieren die
Schritte ab "2. Python-Umgebung einrichten" identisch in einer VM.

## 1. Container in Proxmox anlegen

**Über die Weboberfläche:**

1. Ubuntu-24.04-Template herunterladen, falls noch nicht vorhanden:
   *Proxmox-Weboberfläche → dein Storage (z. B. `local`) → CT Templates →
   Templates → `ubuntu-24.04-standard` herunterladen.*
2. *Datacenter → dein Node → CT erstellen*:
   - **Hostname**: z. B. `news-to-wp`
   - **Template**: `ubuntu-24.04-standard`
   - **Disk**: 8 GB reichen dicke
   - **CPU**: 1-2 Kerne
   - **RAM**: 512 MB - 1 GB
   - **Netzwerk**: an dein bestehendes VLAN/Bridge (z. B. dasselbe wie
     deine anderen Container hinter OPNsense/HAProxy), feste IP oder
     DHCP-Reservierung in OPNsense
   - **Unprivileged container**: ja (Standard, sicherer)
3. Container starten, dann per `pct enter <VMID>` (auf dem Proxmox-Host) oder
   per SSH in den Container.

**Alternativ per CLI auf dem Proxmox-Host** (Beispiel, ID/Netz anpassen):

```bash
pveam update
pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst

pct create 150 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname news-to-wp \
  --cores 2 --memory 1024 --swap 512 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=0 \
  --start 1

pct enter 150
```

## 2. Python-Umgebung einrichten

Im Container (als root oder mit sudo):

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip
```

Ubuntu 24.04 bringt Python 3.12 mit - passt für dieses Tool (Python 3.10+
wird vorausgesetzt).

## 3. Tool-Dateien in den Container bringen

Du hast das Projekt aktuell unter `C:\t\news-to-wp` auf deinem Windows-PC.
Ein paar Wege, es in den Container zu bekommen (du hast bereits FileZilla
installiert, das eignet sich gut dafür):

**Per SFTP/FileZilla:**
1. Im Container einen SSH-Server sicherstellen (bei Ubuntu-Templates meist
   vorinstalliert; sonst `apt install -y openssh-server`).
2. Mit FileZilla per SFTP zur Container-IP verbinden, den kompletten Ordner
   `news-to-wp` z. B. nach `/opt/news-to-wp` hochladen.

**Per SCP (Windows-Terminal/PowerShell mit installiertem OpenSSH-Client):**
```powershell
scp -r C:\t\news-to-wp root@CONTAINER-IP:/opt/news-to-wp
```

**Alternativ**: Projekt zuerst in ein privates Git-Repo legen und im Container
klonen (`git clone ...`) - sauberer für spätere Updates, aber optional.

## 4. Virtuelle Umgebung & Abhängigkeiten

Im Container:

```bash
cd /opt/news-to-wp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Eine `.env`-Datei musst du **nicht** von Hand anlegen - das Tool startet
auch ganz ohne. Alle Zugangsdaten (WordPress, Groq, Pexels, Login-Schutz,
Telegram, Cronjob-Suchbegriffe usw.) trägst du nach dem ersten Start bequem
über die Weboberfläche ein (Button "⚙️ Einstellungen").

## 5. Kurzer Testlauf

```bash
source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Im Browser auf deinem PC: `http://CONTAINER-IP:8000` öffnen, "⚙️
Einstellungen" anklicken, WordPress-/Groq-/Pexels-Zugangsdaten eintragen,
speichern, mit "🔌 Verbindungen testen" prüfen. Mit `Strg+C` im Container
wieder stoppen, sobald es läuft.

## 6. Dauerbetrieb per systemd (Weboberfläche)

Datei `/etc/systemd/system/news-to-wp.service` anlegen:

```ini
[Unit]
Description=News to WordPress Tool (Weboberflaeche)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/news-to-wp
ExecStart=/opt/news-to-wp/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=www-data

[Install]
WantedBy=multi-user.target
```

`User=www-data` ist ein sinnvoller, nicht-root Benutzer - wichtig ist nur,
dass er Schreibrechte im `/opt/news-to-wp`-Ordner hat (für `.env`,
`posted_history.json`, `app.log`):

```bash
chown -R www-data:www-data /opt/news-to-wp
```

Aktivieren:

```bash
systemctl daemon-reload
systemctl enable --now news-to-wp.service
systemctl status news-to-wp.service
```

Der Dienst bindet hier bewusst nur an `127.0.0.1` (nicht von außen direkt
erreichbar) - Zugriff von deinem PC aus läuft über den Reverse Proxy (siehe
Schritt 8), was auch gleich TLS und ggf. eine zusätzliche Auth-Schicht davor
mitbringt. Willst du stattdessen direkt im LAN erreichbar sein (z. B. nur
für dich, ohne HAProxy), ändere `--host 127.0.0.1` auf `--host 0.0.0.0` und
aktiviere unbedingt den Login-Schutz in den Einstellungen.

## 7. Automatisierter Modus per systemd-Timer (statt Cron)

Auf Ubuntu ist ein systemd-Timer die modernere Alternative zu einem
klassischen Cronjob (Logging über `journalctl`, robustere Fehlerbehandlung).

`/etc/systemd/system/news-to-wp-cron.service`:

```ini
[Unit]
Description=News to WordPress Tool (automatischer Post-Lauf)

[Service]
Type=oneshot
WorkingDirectory=/opt/news-to-wp
ExecStart=/opt/news-to-wp/venv/bin/python cron_post.py
User=www-data
```

`/etc/systemd/system/news-to-wp-cron.timer`:

```ini
[Unit]
Description=Startet den News-to-WordPress-Cronjob regelmaessig

[Timer]
OnBootSec=10min
OnUnitActiveSec=4h
Persistent=true

[Install]
WantedBy=timers.target
```

Aktivieren:

```bash
systemctl daemon-reload
systemctl enable --now news-to-wp-cron.timer
systemctl list-timers news-to-wp-cron.timer   # nächste geplante Ausführung prüfen
journalctl -u news-to-wp-cron.service -f      # Logs live mitlesen
```

`OnUnitActiveSec=4h` = alle 4 Stunden; nach Bedarf anpassen. Genau wie bei
der Weboberfläche werden die Suchbegriffe (`NEWS_QUERIES`) und Standard-
Kategorien/Tags für den Cronjob über die Einstellungen-Seite gepflegt - der
Timer nutzt dieselbe `.env`.

## 8. Erreichbarkeit über OPNsense/HAProxy (optional, aber empfohlen)

Da du OPNsense mit HAProxy bereits im Einsatz hast: leg dort ein neues
Backend auf `CONTAINER-IP:8000` an (wenn der Dienst wie in Schritt 6 nur auf
`127.0.0.1` lauscht, `--host 0.0.0.0` verwenden, damit HAProxy von außerhalb
des Containers zugreifen kann) und einen Frontend-Eintrag für z. B.
`news.deine-domain.de` mit TLS-Zertifikat. So bleibt der Port 8000 selbst
nicht direkt aus dem Internet erreichbar, sondern nur über HAProxy mit TLS.

Zusätzlich empfiehlt sich, in den Tool-Einstellungen unter "Login-Schutz"
einen Benutzernamen/Passwort zu setzen, falls die Seite nicht ausschließlich
über VPN erreichbar ist.

## 9. Updates einspielen

Änderst du den Code später erneut (z. B. über eine neue Version dieses
Tools), reicht:

```bash
# neue Dateien in /opt/news-to-wp hochladen (FileZilla/SCP/git pull)
systemctl restart news-to-wp.service
```

Die `.env`, `posted_history.json` und `app.log` bleiben davon unberührt,
solange du sie nicht überschreibst.
