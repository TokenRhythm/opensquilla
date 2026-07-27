<!-- Übersetzt aus README.md @ 8794ffbe. Maßgeblich ist das englische README. -->
<!-- Aktualität prüfen: git log 8794ffbe..HEAD -- README.md -->

# OpenSquilla — Token-effizienter AI Agent

<p align="center">
  <img src="assets/opensquilla-long-logo.png" alt="OpenSquilla logo" width="500">
</p>

<p align="center">
  <b>Gleiches Budget – lass deinen Agent mehr und Besseres leisten.</b><br>
  Mikrokernel-AI-Agent – intelligentes Routing, persistentes Gedächtnis, sichere Sandbox, integrierte Suche und lokale Embeddings.
</p>

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/opensquilla/opensquilla/ci.yml?style=for-the-badge" alt="CI"></a>
  <a href="https://opensquilla.ai/"><img src="https://img.shields.io/badge/website-opensquilla.ai-blue?style=for-the-badge" alt="Website"></a>
  <a href="https://github.com/opensquilla/opensquilla/releases"><img src="https://img.shields.io/github/v/release/opensquilla/opensquilla?include_prereleases&style=for-the-badge" alt="GitHub release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=for-the-badge" alt="Apache 2.0 License"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-Hans.md">中文</a> · <a href="README.ja.md">日本語</a> · <a href="README.fr.md">Français</a> · <b>Deutsch</b> · <a href="README.es.md">Español</a>
</p>

> Dieses Dokument wurde aus dem englischen [`README.md`](README.md) übersetzt; bei Abweichungen ist die englische Fassung maßgeblich.

---

## Neuigkeiten

- 📢 **2026-07-03** — Unser technischer Bericht **[Agentic Routing: The Harness-Native Data Flywheel](docs/releases/agentic_routing_v0.pdf)** (Vorschau) ist erschienen und wurde zusammen mit OpenSquilla **0.5.0 Preview 1** veröffentlicht. Er beschreibt, wie der harness-native Router alltäglichen Agent-Traffic in ein sich selbst verbesserndes Daten-Schwungrad verwandelt.

---

## Überblick

OpenSquilla ist ein Token-effizienter Mikrokernel-AI-Agent. Ein lokaler
Modell-Router schickt jeden Turn an das günstigste Modell, das ihn
bewältigen kann; dauerhaftes Gedächtnis, eine geschichtete Sandbox,
integrierte Websuche und Embeddings auf dem Gerät ergänzen eine einzige
gemeinsame Turn-Schleife.

Jeder Einstiegspunkt — Web UI, CLI und Chat-Kanäle — läuft durch
dieselbe Schleife, sodass sich Tool-Dispatch, Wiederholungsversuche und
Entscheidungs-Logging überall identisch verhalten. Eine modulare
Provider-Schicht spricht mit TokenRhythm, OpenRouter, OpenAI, Anthropic, Ollama,
DeepSeek, Gemini, Qwen/DashScope und über 20 weiteren LLM-Providern —
ohne Änderung an deinem Code oder deinem Konfigurationsschema.

OpenSquilla 0.5.0 ist die aktuelle stabile Version.

Für aufgabenorientierte Produktdokumentation beginnst du am besten mit
dem [OpenSquilla-Produktleitfaden](README.product.md) oder dem
[Dokumentationsindex](docs/README.md).

---

## Installation

OpenSquilla läuft unter Windows, macOS und Linux. Wähle den Weg, der zu
deinem Einsatzzweck passt.

Desktop-Installationsprogramme und die schnelle Terminal-Installation liefern dir
ein vorgefertigtes **Release** — kein
Git erforderlich. Die beiden anderen — Aus Quellcode installieren und
Aus Quellcode entwickeln — bauen **aus einem Git-Checkout** (`git clone`
+ Git LFS), einschließlich der Vue-Konsole. Release-Wheels und
Desktop-Installationsprogramme enthalten die Konsole bereits; ihre Nutzer
benötigen weder Node.js noch npm.

Release-Installationsbefehle verwenden veröffentlichte GitHub-Release-Assets.
Python-Wheel-Installationen verwenden versionsbehaftete Wheel-Dateinamen,
weil die Installationsprogramme die im Wheel-Dateinamen eingebettete
Version prüfen.

Für den Desktop-Einsatz von 0.5.0 bevorzugst du die gepackten
Desktop-Installationsprogramme aus dem GitHub-Release:
`OpenSquilla-0.5.0-mac-arm64.dmg` unter macOS und
`OpenSquilla-0.5.0-win-x64.exe` unter Windows.

| Weg | Zielgruppe | Wann verwenden |
| --- | --- | --- |
| [Desktop-Installationsprogramme](#desktop-installers) **(empfohlen für Desktop)** | macOS- und Windows-Nutzer | Gepackte Desktop-App |
| [Schnelle Terminal-Installation](#quick-terminal-install) **(empfohlen)** | Endnutzer auf jedem Betriebssystem | Release-Wheel aus dem Terminal |
| [Aus Quellcode installieren](#install-from-source) | Nutzer, die `main` verfolgen | Aus einem Checkout ausführen, nicht bearbeiten |
| [Aus Quellcode entwickeln](#develop-from-source) | Mitwirkende | Quellcode bearbeiten, testen oder debuggen |

### Voraussetzungen

| Anforderung | Schnelle Terminal-Installation | Aus Quellcode installieren | Aus Quellcode entwickeln |
| --- | :---: | :---: | :---: |
| Python 3.12+ | über `uv` | über `uv` oder System | über `uv` |
| Git + Git LFS | — | erforderlich | erforderlich |
| Node.js 22.12+ + npm | — | zum Bauen der Web UI erforderlich | für Web UI und Wheels erforderlich |
| `uv` | wird bei Bedarf installiert | empfohlen | erforderlich |

Das Standardprofil `recommended` installiert **SquillaRouter** —
OpenSquillas Modell-Router auf dem Gerät — und seine Modell-Assets;
`OPENSQUILLA_INSTALL_PROFILE=core` lässt diese Abhängigkeiten weg. Das
separate Onboarding-Flag `--router disabled` behält die installierten
Abhängigkeiten bei, schaltet den Router aber zur Laufzeit ab.

Unter Windows benötigt die mit SquillaRouter gebündelte ONNX-Runtime
zusätzlich die Visual-C++-Runtime. Das PowerShell-Installationsprogramm für die
Quellcode-Installation installiert sie automatisch über `winget`; der Weg über die
**schnelle Terminal-Installation** (`uv tool install`) tut das nicht —
falls beim Start ein `DLL load failed`-Fehler protokolliert wird,
installiere sie manuell (siehe [Fehlerbehebung](#troubleshooting)).
OpenSquilla läuft mit direktem Single-Model-Routing weiter, bis sie
installiert ist.

Bei Terminal-Installationen unter macOS benötigt die LightGBM-Runtime
von SquillaRouter möglicherweise zusätzlich die OpenMP-Systembibliothek.
Die Desktop-App bringt die benötigte Runtime mit, aber die
**schnelle Terminal-Installation** installiert keine
Homebrew-/Systembibliotheken. Falls beim Start `Library not loaded:
@rpath/libomp.dylib` protokolliert wird, führe `brew install libomp` aus
und starte dann das Gateway neu. OpenSquilla läuft mit direktem
Single-Model-Routing weiter, bis sie installiert ist.

Installationslinks: [Git](https://git-scm.com/downloads) ·
[Git LFS](https://git-lfs.com/) ·
[Node.js](https://nodejs.org/en/download) ·
[uv](https://docs.astral.sh/uv/getting-started/installation/).

<a id="desktop-installers"></a>

### Desktop-Installationsprogramme

Die 0.5.0-Preview-4-Desktop-Installationsprogramme bündeln die Vue-Steuerkonsole
und die Gateway-Runtime in einer Electron-Hülle.

- macOS Apple Silicon: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg>
- Windows x64: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe>

Für schnellere Downloads in Festlandchina verwende die direkten OSS-Download-Aliasse:
- macOS Apple Silicon: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-mac-arm64.dmg>
- Windows x64: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe>

Diese festen Links werden nur weitergeschaltet, wenn ein neueres geeignetes Release die Spiegelprüfung bestanden hat. Verwende die oben stehenden versionierten GitHub-Release-Links, wenn du eine bestimmte Version benötigst.

Beende vor dem Upgrade jede laufende OpenSquilla-Desktop-App. Das vorhandene
Desktop-Profil im Anwendungsdatenverzeichnis der Plattform wird weiterverwendet.
`~/.opensquilla` der Terminal-Installation ist ein separates Profil; übertrage es
bei Bedarf ausdrücklich über die Einstellungen.

Führe beim Upgrade der Windows-Desktop-App von RC3 auf RC4 oder neuer das neue
Installationsprogramm direkt über der vorhandenen Installation aus. Deinstalliere
RC3 nicht zuerst: Das RC3-Deinstallationsprogramm kann Desktop-Benutzerdaten löschen.
Sichere vor dem Upgrade `%APPDATA%\OpenSquilla`. Installationsprogramme ab RC4
behalten Profildaten bei einer normalen Deinstallation bei.

<a id="quick-terminal-install"></a>

### Schnelle Terminal-Installation

Der empfohlene Weg unter Windows, macOS und Linux. `uv` installiert
OpenSquilla in eine eigene, isolierte Umgebung und verwaltet sein
eigenes Python — kein System-Python erforderlich. Dieser Weg
installiert nur veröffentlichte Releases; für `main`,
Entwicklungs-Branches oder lokale Checkouts nutze
[Aus Quellcode installieren](#install-from-source).

**1. `uv` installieren** — überspringen, falls `uv --version` bereits
funktioniert.

Linux / macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
```

**2. OpenSquilla installieren** — derselbe Befehl auf jeder Plattform.

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

Damit wird das OpenSquilla-Wheel von der Release-URL installiert;
anschließend lässt `uv` die von den gewählten Extras deklarierten
Abhängigkeiten herunterladen. Das Standard-Extra `recommended` enthält
SquillaRouter-Runtime-Abhängigkeiten wie ONNX Runtime, LightGBM, NumPy
und tokenizers, sodass eine Erstinstallation Netzwerkzugriff benötigt,
sofern diese Wheels nicht bereits zwischengespeichert sind. `uv`
installiert keine nativen Systemruntimes wie macOS `libomp` oder das
Windows Visual C++ Redistributable; siehe
[Fehlerbehebung](#troubleshooting), falls die Router-Runtime einen
Ladefehler einer nativen Bibliothek meldet.

**3. Konfigurieren und ausführen.**

```sh
opensquilla onboard
opensquilla gateway run
```

> [!NOTE]
> Wird `opensquilla` direkt nach einer frischen `uv`-Installation nicht
> gefunden, öffne ein neues Terminal oder führe die PATH-Zeile aus
> Schritt 1 erneut aus.

Für eine vollständig festgelegte Installation verwende die
versionsbehaftete Wheel-URL:
`https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl`.

<a id="install-from-source"></a>

### Aus Quellcode installieren

Nutze diesen Weg, um OpenSquilla aus einem Checkout auszuführen, ohne
ihn zu bearbeiten. Der Klon dient dem Installationsprogramm nur als
Paketquelle; verwende nach der Installation den `opensquilla`-Befehl —
führe nicht `uv run` aus. Wähle stattdessen
[Aus Quellcode entwickeln](#develop-from-source), wenn du den Code
ändern möchtest.

1. **Mit LFS-Assets klonen**

   ```sh
   git lfs install
   git clone https://github.com/opensquilla/opensquilla.git
   cd opensquilla
   git lfs pull --include="src/opensquilla/squilla_router/models/**"
   ```

2. **Installationsprogramm ausführen**

   **macOS / Linux**

   ```sh
   bash scripts/install_source.sh
   ```

   **Windows PowerShell**

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1
   ```

   Das Skript führt zuerst `npm ci` und `npm run build` in
   `opensquilla-webui` aus und installiert danach `.[recommended]` (SquillaRouter + Gedächtnis +
   lokale Modelle) über `uv tool install` in eine dedizierte
   Benutzerumgebung und fällt auf `python -m pip install --user` zurück,
   wenn `uv` nicht verfügbar ist. Bei jeder Neuinstallation aus dem Quellcode
   erstellt `npm ci` den `node_modules`-Baum neu und baut die Konsole erneut. Der
   erste Lauf lädt normalerweise am meisten; ein warmer npm-Cache reduziert später
   den Netzwerkverkehr, aber nicht die gesamte Build-Zeit oder alle Schreibvorgänge. Öffne ein neues Terminal, falls
   `opensquilla` nach der Installation nicht im `PATH` liegt.

   Direkte Aufrufe von `pip install .`, `uv tool install .` und Installationen
   über eine VCS-URL sind Low-Level-Quellbuilds und ersetzen dieses Skript nicht.
   Ein lokaler Checkout benötigt zuerst ein gebautes Web UI; ein VCS-Checkout
   enthält kein generiertes Artefakt und wird absichtlich abgelehnt. Verwende
   das Quellinstallationsskript oder ein offizielles Release-Wheel.

3. **(optional) Fortgeschrittene Extras installieren.** Die meisten
   Kanäle — Feishu, Telegram, DingTalk, QQ, WeCom, Slack und Discord —
   funktionieren mit der Basisinstallation. Die optionalen Extras sind:

   - `matrix` — Matrix-Kanal (zieht `matrix-nio` mit ein)
   - `matrix-e2e` — Matrix-Kanal mit Ende-zu-Ende-Verschlüsselung
     (erfordert libolm)
   - `document-extras` — PDF-Erzeugung über WeasyPrint

   ```sh
   OPENSQUILLA_INSTALL_EXTRAS=matrix bash scripts/install_source.sh        # macOS / Linux
   ```

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Extras matrix   # Windows
   ```

4. **Konfigurieren und ausführen** — siehe [Konfiguration](#configuration).

<details>
<summary>Aus Quellcode installieren — Terminal-Voraussetzungen und Installationsoptionen</summary>

**Voraussetzungen (Git, Git LFS, Node.js 22.12+ mit npm, uv) aus einem Terminal installieren**

Windows PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.GitLFS -e
winget install --id OpenJS.NodeJS.LTS -e
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
git lfs install
```

macOS (Homebrew):

```sh
brew install git git-lfs node uv
git lfs install
```

Debian / Ubuntu:

```sh
sudo apt update && sudo apt install -y git git-lfs curl
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
git lfs install
```

Unter Fedora verwende `sudo dnf install -y git git-lfs`; unter Arch
`sudo pacman -S --needed git git-lfs`; installiere außerdem Node.js 22.12+
und npm über die Distribution oder nodejs.org und `uv` anschließend mit
dem obigen `curl`-Befehl. PATH-Änderungen dieser Installationsprogramme
gelten für neue Terminal-Sitzungen.

**Umgebungsvariablen des Installationsprogramms und PATH-Prüfungen**

```sh
OPENSQUILLA_INSTALL_PROFILE=core   bash scripts/install_source.sh   # minimale Runtime, kein SquillaRouter
OPENSQUILLA_INSTALL_DRY_RUN=1      bash scripts/install_source.sh   # nur den Plan ausgeben
```

Prüfe mit `command -v opensquilla` (macOS/Linux) oder
`where.exe opensquilla` (Windows), welches `opensquilla` deine Shell
tatsächlich ausführt. Liegt es nicht im `PATH`, führe
`uv tool update-shell` aus. Starte das Gateway nach einer
Neuinstallation aus einem lokalen Checkout neu, damit es das
aktualisierte Paket lädt.

</details>

<a id="develop-from-source"></a>

### Aus Quellcode entwickeln

Nutze diesen Weg, wenn du am Quellcode von OpenSquilla arbeitest:
Änderungen vornehmen, Tests ausführen oder Verhalten gegen diesen
Checkout debuggen. Es ist nicht der normale Installationsweg. Anders als
[Aus Quellcode installieren](#install-from-source) erfordert dieser Weg
`uv`: `uv sync` legt ein repository-lokales `.venv` an, und `uv run`
führt Befehle gegen die Dateien in diesem Checkout aus.

```sh
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra recommended --extra dev
uv run opensquilla --help
```

Führe `npm run build` nach Änderungen an der Web UI erneut aus. Ein normaler
Wheel-Build schlägt bei fehlender oder veralteter Konsole fehl; ein editierbares
`uv sync` bleibt für reine Backend-Arbeit verfügbar.

Das Extra `recommended` enthält SquillaRouter auch für die Entwicklung;
das Extra `dev` installiert die Test-, Lint- und Typecheck-Werkzeuge.
Installiere zusätzliche Extras in dieselbe Umgebung, die du ausführst:

```sh
uv sync --extra recommended --extra dev --extra matrix
uv run opensquilla channels status matrix --json
```

Setze in diesem Modus jedem `opensquilla`-Befehl in der
[Konfiguration](#configuration) ein `uv run` voran. Debugge einen
Entwicklungs-Checkout nicht über einen benutzerlokalen
`opensquilla`-Befehl — dieser Befehl läuft in einer anderen
Python-Umgebung.

### Deinstallieren

Entferne OpenSquilla mit `opensquilla uninstall`. Standardmäßig bleiben
deine Daten erhalten und nur das Programm wird entfernt:

```sh
opensquilla uninstall --dry-run   # vorab anzeigen, was entfernt und behalten würde
opensquilla uninstall             # Programm entfernen, Daten behalten
```

Um auch Daten zu löschen, entscheide dich ausdrücklich dafür:

```sh
opensquilla uninstall --purge-state    # Sitzungen, Logs, Cache, Scheduler, Gedächtnis
opensquilla uninstall --purge-config   # config.toml und Geheimnisse (.env)
opensquilla uninstall --purge-all      # alles (verlangt eine Eingabe zur Bestätigung)
```

Das laufende Gateway wird zuerst geleert und gestoppt, das Löschen
bleibt innerhalb des OpenSquilla-Stammverzeichnisses, und für
Docker-/Desktop-Installationen werden stattdessen geführte
Entfernungsschritte angeboten. Die vollständige Referenz findest du in
[`docs/cli.md`](docs/cli.md#uninstall).

---

## Installationsdatenschutz

OpenSquilla verwendet anonyme Installationstelemetrie, um die Anzahl der Installationen,
die Verbreitung von Versionen und die Laufzeitkompatibilität abzuschätzen. Die Daten
werden nur beim ersten Start des Gateways gesendet und für jede OpenSquilla-Version nur
einmal. Außerdem fasst OpenSquilla lokal nach UTC-Datum die Anzahl abgeschlossener
Dialogrunden auf oberster Ebene und die Token-Nutzung zusammen. Beim Start sowie
anschließend stündlich versucht OpenSquilla, noch ausstehende kumulierte Tagesstände an
denselben Telemetriedienst zu übermitteln. OpenSquilla kann auch passive
Aktualisierungsprüfungen durchführen, darunter beim Start der Desktop-Anwendung und
während ihres Betriebs höchstens einmal täglich. Uploads verwenden ein kurzes Timeout
und blockieren den Start nie.

Was gesendet wird:

- Schemaversion
- lokal erzeugter, stabiler `install_id`-Digest
- OpenSquilla-Version
- Ereignistyp (`install`, `version_seen` oder `daily_usage`)
- Installationsmethode (`pip`, `source`, `docker`, `desktop` oder `unknown`)
- Betriebssystem, Betriebssystemversion, CPU-Architektur sowie
  Python-Haupt-/Nebenversion
- Zeitstempel des ersten Auftretens und des Versands
- CI-/Testumgebungs-Marker (`ci_environment`)
- bei täglichen Nutzungsereignissen: UTC-Datum, Anzahl abgeschlossener Dialogrunden
  sowie Summen der Input-, Output-, Cache- und Cache-Write-Tokens

Die `install_id` ist ein lokaler, einseitiger SHA-256-Digest, abgeleitet
aus nutzbaren MAC-Adressen, dann aus lokalen IP-Adressen, wenn keine MAC
verfügbar ist, mit einem zufälligen, dauerhaft gespeicherten Fallback.
Rohe MAC-/IP-Werte werden nicht hochgeladen.

Was nicht gesendet wird: Benutzernamen, Hostnamen, Pfade, API-Keys,
Provider-Konfiguration, Chat-/Sitzungs-/Gedächtnis-/Agent-Inhalte,
Dateinamen oder Dateiinhalte. Die Quell-IP kann für HTTP-Server auf der
Transportschicht sichtbar sein, ist aber nicht Teil der Nutzlast.

Um vor dem Start alle nicht vom Benutzer ausgelösten Netzwerkbeobachtungen zu
deaktivieren:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

Oder in der Konfiguration:

```toml
[privacy]
disable_network_observability = true
```

Dieser einheitliche Schalter umfasst die automatische Installationstelemetrie, die
täglich zusammengefasste Nutzungstelemetrie, passive Aktualisierungsprüfungen sowie
automatische Aktualisierungsprüfungen beim Start der Desktop-Anwendung und während
ihres Betriebs. Solange der einheitliche oder ein kompatibler älterer
Deaktivierungsschalter aktiv ist, können auch ausdrücklich vom Benutzer ausgelöste
Prüfungen auf verfügbare Aktualisierungen ihn nicht umgehen. Andere vom Benutzer
ausgelöste Aktionen können nach eindeutiger Absicht weiterhin auf Netzwerkdienste
zugreifen, etwa beim Öffnen der Release-Seite, beim Herunterladen von Release-Dateien
oder bei der Nutzung konfigurierter Provider, Suchdienste oder Kanäle.

Ältere Umgebungsvariablen werden weiterhin unterstützt:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

Fortgeschrittene Bereitstellungen können einen eigenen Installationstelemetrie-Endpunkt
verwenden:

```sh
OPENSQUILLA_TELEMETRY_ENDPOINT=https://example.com/v1/install
```

---

<a id="configuration"></a>

## Konfiguration

### Ersteinrichtung

`opensquilla onboard` ist der interaktive Assistent für die
Ersteinrichtung. Er schreibt die aktive Konfigurationsdatei und behält
Provider-Geheimnisse in Umgebungsvariablen, wenn du `--api-key-env`
angibst. Der Router ist standardmäßig auf `recommended` gesetzt
(SquillaRouter auf unterstützten Providern); gib `--router disabled` an
für direktes Single-Model-Routing.

```sh
opensquilla onboard                # vollständiger interaktiver Assistent
opensquilla onboard --if-needed    # idempotent: sicher für Skripte und Neuinstallationen
opensquilla onboard --minimal      # nur Provider; Kanäle und Suche überspringen
opensquilla onboard status         # jeden Einrichtungsabschnitt prüfen, ohne zu schreiben
```

Verwende in SSH, CI oder jeder Umgebung ohne TTY die nicht-interaktive
Form — behalte das Geheimnis in der Umgebung und übergib seinen
**Namen**, nicht seinen Wert:

**Linux / macOS**

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

**Windows PowerShell**

```powershell
$env:OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

OpenRouter ist nur ein Beispiel — ersetze es durch einen beliebigen
unterstützten Provider und dessen API-Key-Variable.

Konfiguriere später einen einzelnen Abschnitt neu, ohne den gesamten
Assistenten zu wiederholen (diese Beispiele setzen voraus, dass der
betreffende API-Key bereits in der Umgebung vorhanden ist):

```sh
opensquilla configure provider --provider openai --model gpt-4o --api-key-env OPENAI_API_KEY
opensquilla configure router --router recommended
opensquilla configure search   --search-provider duckduckgo
opensquilla configure search   --search-provider exa --api-key-env EXA_API_KEY
opensquilla configure channels
```

Abschnitte: `provider`, `router`, `channels`, `search`,
`image-generation`, `memory-embedding`. Die Web UI stellt denselben
Katalog und dasselbe Statusmodell unter `/control/setup` bereit:
Provider und Router sind der schnelle Weg, während Channels, Search,
Image generation und Memory embedding im Capability Center liegen und
später konfiguriert werden können. Leere Kanäle gelten als bewusstes
Auslassen, nicht als fehlgeschlagene Einrichtung.

**Ladereihenfolge der Konfiguration:** `OPENSQUILLA_GATEWAY_CONFIG_PATH`
→ `./opensquilla.toml` → `~/.opensquilla/config.toml` → eingebaute
Standardwerte. Umgebungswerte einzelner Geheimnisse haben stets Vorrang
vor Dateiwerten.

### Von OpenClaw oder Hermes Agent migrieren

Falls du bereits Zustandsdaten unter `~/.openclaw` oder `~/.hermes`
hast, führe zuerst einen Probelauf aus, um den Migrationsbericht zu
prüfen, und wende ihn dann ausdrücklich an:

```sh
opensquilla migrate openclaw --json
opensquilla migrate openclaw --apply

opensquilla migrate hermes --json
opensquilla migrate hermes --apply
```

Verwende `opensquilla migrate --source openclaw,hermes --apply`, um
beide Standard-Stammverzeichnisse zu importieren. Füge
`--migrate-secrets` erst hinzu, nachdem du den Probelaufbericht geprüft
hast. Für benutzerdefinierte Pfade und Konfliktbehandlung siehe
[`MIGRATION.md`](MIGRATION.md).

### Ausführen

```sh
opensquilla gateway run                # Vordergrund, 127.0.0.1:18791
opensquilla gateway start --json       # Hintergrund + Warten auf Health
opensquilla chat                       # interaktive REPL
opensquilla agent -m "dein Prompt"     # einmalig, automatisierungsfreundlich
```

Öffne die Web UI unter <http://127.0.0.1:18791/control/>. Die Ansicht
**Health** zeigt, ob OpenSquilla bereit ist, was nicht bereit ist und
die nächsten Schritte zur Wiederherstellung. Führe in der CLI aus:

```sh
opensquilla doctor
opensquilla doctor --json
opensquilla doctor --config ./opensquilla.toml --json
```

`/health` und `/healthz` sind leichtgewichtige Liveness-Endpunkte für
Prozessprüfungen. `opensquilla doctor` und die Health-Ansicht der Web UI
sind die Readiness-Oberflächen für Provider-Konfiguration, Gedächtnis,
Logs, Suche, Kanäle, Sandbox-Haltung, Router, Bildgenerierung und
Wiederherstellungshinweise. Drücke `Ctrl+C`, um ein Vordergrund-Gateway
zu stoppen.

Weitere Befehlsgruppen sind unter anderem `sessions`, `skills`,
`memory`, `migrate`, `cron`, `channels`, `providers`, `models` und
`cost`. Führe `opensquilla --help` oder `opensquilla <gruppe> --help`
für Details aus.

<details>
<summary>Fortgeschrittene Konfiguration — einen Kanal verifizieren, Bindung ans öffentliche Netz, Docker</summary>

**Einen Messaging-Kanal verbinden und verifizieren**

Das Speichern eines Kanals ist eine Konfigurationsänderung, kein Beleg
für die Konnektivität zur Laufzeit. Starte das Gateway nach
Kanal-Änderungen neu und verifiziere dann den aktiven Kanal:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

Betrachte einen Kanal nur dann als verbunden, wenn die Status-Nutzlast
`enabled=true`, `configured=true` und `connected=true` meldet. Feishu
verwendet standardmäßig den Websocket-Modus, Telegram Polling, und Slack
kann den Socket Mode nutzen — keiner dieser Modi benötigt eine
öffentliche URL. Der Feishu-Webhook-Modus, der Telegram-Webhook-Modus,
der Slack-Webhook-Modus und WeCom erfordern eine öffentliche, vom
Provider erreichbare URL.

**Bindung ans öffentliche Netz**

Um die Web UI von einer anderen Maschine zu erreichen, binde das Gateway
an alle Schnittstellen und verwende die öffentliche IP des Hosts:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Öffentlicher Zugriff erfordert außerdem, dass die Host-Firewall oder die
Cloud-Sicherheitsgruppe eingehendes TCP auf diesem Port erlaubt. Mache
das Gateway nicht mit `[auth] mode = "none"` öffentlich zugänglich —
konfiguriere Token-Authentifizierung, bevor du an `0.0.0.0` bindest.

**Docker**

Vorgebaute Multi-Arch-Images (`amd64`/`arm64`) werden mit jedem
Release-Tag auf `ghcr.io/opensquilla/opensquilla` veröffentlicht —
[`docs/docker.md`](docs/docker.md) ist der vollständige
Container-Leitfaden (Heimserver und NAS, LAN-Zugriff mit
Token-Authentifizierung, Upgrades):

```sh
OPENSQUILLA_GATEWAY_IMAGE=ghcr.io/opensquilla/opensquilla:latest docker compose up -d
```

Ohne `OPENSQUILLA_GATEWAY_IMAGE` führt der Compose-Weg ein
`opensquilla:local`-Image aus, das du selbst
baust. Baue es aus einem Quellcode-Checkout mit den per Git LFS
geholten Router-Assets (Klon und `git lfs pull` siehe
[Aus Quellcode installieren](#install-from-source)):

```sh
docker build -t opensquilla:local .
```

`./start.sh` (oder `start.ps1` unter Windows) führt dann
`docker compose up -d` aus und folgt den Gateway-Logs. Docker erspart
eine Python-Toolchain auf dem Host — nicht den lokalen Image-Build.

</details>

Provider-Tiers, Sandbox-Feinabstimmung, Bildgenerierung und
Nebenläufigkeitseinstellungen liegen in `opensquilla.toml.example`.

---

## Neuerungen in 0.4.1

OpenSquilla 0.4.1 ist ein Wartungsrelease für die Desktop- und
Control-UI-Linie:

- **Desktop-Zuverlässigkeit** – die Prüfungen des gepackten Gateways
  decken nun den Coding-Modus, `code-task` und den SquillaRouter-Start
  ab, und das Handling von Desktop-Fenstern/Artefakten ist stabiler.
- **Sechssprachige Client-Unterstützung** – die Control UI und der
  Desktop-Client unterstützen Englisch, vereinfachtes Chinesisch,
  Japanisch, Französisch, Deutsch und Spanisch über First-Paint- und
  Einstellungsoberflächen hinweg.
- **Coding-Modus und Router-Paketierung** – Desktop-Builds schlagen
  schnell fehl, wenn Router-Assets fehlen oder noch Git-LFS-Pointer sind,
  und verhindern so beeinträchtigte Release-Pakete.
- **Telemetrie und Windows-Feinschliff** – die Installationstelemetrie
  überspringt CI- und Testumgebungen, und Windows-Desktop-Assets
  verwenden das OpenSquilla-Logo.
- **Mainline-Governance** – gewöhnliche Pull Requests und die
  Release-Integration sind um `main` herum ausgerichtet, während
  Maintainer-Branches für Release-, Hotfix-, Staging-, Integrations- und
  Sandbox-Arbeit reserviert sind.

Vollständige Hinweise: [`CHANGELOG.md`](CHANGELOG.md) ·
[`docs/releases/0.4.1.md`](docs/releases/0.4.1.md).

## Neuerungen in 0.2.1

OpenSquilla 0.2.1 ist ein Wartungsrelease mit Fokus auf den Start von
Release-Paketen und die Zuverlässigkeit langlaufender Agents:

- **Windows-Portable-Start** – der Portable-Launcher erkennt und bootet
  die vom gebündelten ONNX-Router benötigte Visual-C++-Runtime besser.
- **Langlaufende Agent-Turns** – tool-intensive WebUI-Sitzungen erholen
  sich sauberer von überdimensionierten Tool-Ergebnissen, fehlerhaften
  Tool-Aufrufen, Übergaben bei der Artefaktauslieferung und
  beeinträchtigten finalen Antworten.
- **Sauberere WebUI-Ausgabe** – generierte Artefaktmarker werden aus dem
  normalen Chat-Replay herausgehalten, während ausgelieferte Dateien
  sichtbar bleiben.
- **Bewertung des Gedächtnisabrufs** – lokale und OpenAI-kompatible
  Embedding-Vektoren werden vor der semantischen Suche normalisiert, und
  starke Stichwort-Treffer bleiben nutzbar, wenn Vektorwerte niedrig
  sind.

Vollständige Hinweise: [`CHANGELOG.md`](CHANGELOG.md) ·
[Release-Notizen](https://opensquilla.ai/news/).

## Neuerungen in 0.2.0

Dieses Release erweitert OpenSquilla über Migration, CLI-Chat, Kanäle,
Scheduling und langlaufende Tool-Arbeit hinweg:

- **Migrationsweg aus bestehenden Agent-Stammverzeichnissen** –
  `opensquilla migrate` zeigt Importe aus bestehenden
  OpenClaw-/Hermes-Stammverzeichnissen in der Vorschau und führt sie
  aus, einschließlich Gedächtnis, Persona-Dateien, Skills,
  MCP-/Kanal-Konfiguration, Konfliktbehandlung und Migrationsberichten.
- **Nutzbare Chat-CLI** – `opensquilla chat` hat eine stabile
  Terminal-UI, Streaming-Ausgabe, Eingabe-Queue, Slash-Modus-Discovery,
  Tool-/Status-Leisten und ein deterministischeres Verhalten der
  Live-Eingabeaufforderung.
- **Oberflächenübergreifende Cron-Automatisierung** – Cron-Jobs decken
  nun strukturierte Zeitpläne, zeitzonenbewusste Exact-/Every-/Cron-Läufe,
  Kanal- oder Webhook-Auslieferung, Fehlerziele, manuelle Läufe und
  WebUI-/CLI-/RPC-Parität ab.
- **Bessere Feishu- und Discord-Kanäle** – Kanal-Adapter legen
  klarere Capability-Metadaten, sichereres DM-/Gruppen-Handling, native
  Datei- und Artefaktpfade sowie verbessertes Anhang-/Thread-Verhalten
  offen, während privilegierte Aktionen abgegrenzt bleiben.
- **Robustere langlaufende Turns** – fehlgeschlagene Turns werden aus dem
  Provider-Replay herausgehalten, fehlerhafte Tool-Aufrufe werden
  sicherer behandelt, und freigabepflichtige Wiederholungen warten auf
  die Entscheidung der Operatoren.
- **Intelligenteres Kontext- und Tool-Budgeting** –
  Provider-Budget-Kompaktierung, Bewahrung des Prompt-Caches,
  begrenzte Tool-Ergebnisse und nebenwirkungsbewusste Nebenläufigkeit
  machen große, tool-intensive Sitzungen vorhersehbarer.
- **Feinschliff bei Web UI und Release** – Aktualitätssortierung,
  Tabellenlayout, mobile Steuerelemente, doppelte Benachrichtigungen,
  Einrichtungsformulare, Release-URLs und Installationswege wurden für
  0.2.0 nachgeschärft.

Vollständige Hinweise: [`CHANGELOG.md`](CHANGELOG.md) ·
[Release-Notizen](https://opensquilla.ai/news/).

---

## Hauptfunktionen

| Fähigkeit | Was sie leistet |
| --- | --- |
| **Token-effizientes Routing** | `SquillaRouter` — ein lokaler LightGBM-+-ONNX-Klassifizierer im Extra `recommended` — bewertet jeden Turn nach Länge, Sprache, Code, Stichwörtern und semantischen Embeddings und routet ihn dann über vier Tiers (C0–C3; die alten Namen T0–T3 sind Aliase) zum günstigsten leistungsfähigen Modell. Die Klassifizierung läuft auf dem Gerät; dein Prompt verlässt die Maschine für diese Entscheidung nie. |
| **Adaptives Reasoning und Prompts** | OpenSquilla fordert erweitertes Reasoning nur für Turns an, die der Router als komplex bewertet, und der System-Prompt skaliert mit der Aufgabenkomplexität — schlank für triviale Turns, vollständige Anweisungen für komplexe. |
| **Über 20 LLM-Provider** | Die Provider-Registry zielt auf über 20 LLM-Backends — TokenRhythm, OpenRouter, OpenAI, Anthropic, Ollama, DeepSeek, Gemini, DashScope/Qwen, Moonshot, Mistral, Groq, Zhipu, SiliconFlow, vLLM, LM Studio und mehr — mit Primär-plus-Fallback-Auswahl; das Erst-Onboarding legt die verifizierte Teilmenge offen. |
| **Bedarfsgesteuerte Skills und MCP** | 15 gebündelte Skills (Coding, GitHub, Cron, pptx/docx/xlsx/pdf, Zusammenfassung, tmux, Wetter und mehr) werden nur geladen, wenn die Aufgabe sie braucht. OpenSquilla ist ein MCP-Client und kann auch als MCP-Server laufen — `opensquilla mcp-server run` benötigt das Extra `mcp` (installiere `opensquilla[recommended,mcp]`). Skills lassen sich über die CLI erstellen, installieren und veröffentlichen. |
| **Dauerhaftes lokales Gedächtnis** | Eine kuratierte `MEMORY.md` plus datierte Markdown-Notizen, durchsucht mit SQLite-Volltext-Stichwortsuche und `sqlite-vec`-Semantikabruf. Embeddings laufen über gebündeltes ONNX auf dem Gerät oder wechseln zu OpenAI/Ollama. Optionaler exponentieller Decay und eine aktivierbare „Dream“-Konsolidierung sind verfügbar. |
| **Geschichtete Sicherheits-Sandbox** | Drei Richtlinien-Tiers (Standard / Strict / Locked) auf einer Berechtigungsmatrix. Bubblewrap isoliert die Codeausführung unter Linux; das macOS-Seatbelt-Backend rendert derzeit nur Profile (Ausführung ausstehend), und unter Windows gibt es noch kein Sandbox-Backend. Ein Denial-Ledger pausiert autonome Läufe nach wiederholten Ablehnungen automatisch, abgelehnte Ausgaben werden verworfen, und Skill-Metadaten sowie Tool-Ergebnisse werden gegen Prompt-Injection XML-escaped. |
| **Integrierte Tools** | Datei lesen/schreiben/bearbeiten, Shell- und Hintergrundprozesse, Git, Websuche (DuckDuckGo, Bocha, Brave, Tavily oder Exa) und Fetch hinter einem SSRF-Schutz, Tabellen-/PPTX-/PDF-Erstellung, Bildgenerierung und Text-to-Speech. |
| **Einheitliches Gateway** | Ein Starlette-ASGI-Server unter `127.0.0.1:18791` mit WebSocket-RPC und einer eingebetteten Steuerkonsole (`/control/`). Web UI, CLI und Kanäle für Terminal, WebSocket, Slack, Telegram, Discord, Feishu, DingTalk, WeCom, Matrix und QQ teilen sich alle einen `TurnRunner`. |
| **Dauerhafte Sitzungen, Subagents und Scheduling** | SQLite-gestützte Speicherung von Sitzungen, Transkripten und Replays mit Arbeitsbereichen pro Agent. Agents starten tiefenbegrenzte Subagents, und eine `SchedulerEngine` mit einem in den Code integrierten Cron-Parser führt wiederkehrende Jobs über `opensquilla cron` aus. |
| **Operator-Steuerung** | Human-in-the-Loop-Freigaben können sensible Tool-Aufrufe für eine Entscheidung pausieren; Token- und Kostenaufstellungen pro Turn und pro Sitzung (`opensquilla cost`) sowie Diagnosen sind über CLI und Web UI verfügbar. |

MetaSkill-Doku: [`docs/features/meta-skills.md`](docs/features/meta-skills.md),
[`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md)
und [`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md).

---

## Benchmark-Ergebnisse

PinchBench-1.2.1-Durchschnittsergebnisse über 25 Aufgaben:

| Agent | Basismodell | Ø-Score | Eingabe-Tokens gesamt | Ausgabe-Tokens gesamt | Gesamtkosten |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenSquilla | Modell-Router (Opus4.7, GLM5.1, DS4 Flash) | 0.9251 | 1,721,328 | 61,475 | $0.688 |
| OpenClaw | Claude Opus 4.7 | 0.9255 | 3,066,243 | 50,890 | $6.233 |

Der Score ist der Mittelwert über die 25 Aufgaben; Token-Zahlen und
Kosten sind Summen für den gesamten Lauf.

---

<a id="troubleshooting"></a>

## Fehlerbehebung

<details>
<summary>macOS: <code>Library not loaded: @rpath/libomp.dylib</code></summary>

Wenn beim Start `Library not loaded: @rpath/libomp.dylib` aus
`lightgbm/lib/lib_lightgbm.dylib` protokolliert wird, läuft OpenSquilla
mit direktem Single-Model-Routing weiter, aber die gebündelte
`SquillaRouter`-Runtime bleibt inaktiv, bis die macOS-OpenMP-Runtime
installiert ist.

Die Desktop-App bringt die benötigte native Runtime mit. Wenn
du die schnelle Terminal-Installation oder eine Quellcode-Installation
aus einer Shell verwendet hast, installiere `libomp` mit Homebrew und
starte das Gateway neu:

```sh
brew install libomp
opensquilla gateway restart
```

</details>

<details>
<summary>Windows: <code>DLL load failed</code> / Visual-C++-Runtime</summary>

Wenn beim Start `DLL load failed while importing
onnxruntime_pybind11_state` protokolliert wird, läuft OpenSquilla mit
direktem Single-Model-Routing weiter, aber die gebündelte
`SquillaRouter`-Runtime bleibt inaktiv, bis das Visual C++
Redistributable für Visual Studio 2015–2022 (x64) installiert ist.

Das PowerShell-Installationsprogramm für die Quellcode-Installation versucht,
das Redistributable über `winget` zu installieren. Wenn du die schnelle Terminal-Installation
verwendet hast oder `winget` nicht verfügbar ist, installiere es manuell
und starte PowerShell neu:
<https://aka.ms/vs/17/release/vc_redist.x64.exe>. Stelle anschließend den
empfohlenen Router wieder her:

```powershell
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY --router recommended
opensquilla gateway restart
```

</details>

---

## Danksagungen

OpenSquilla ist inspiriert von
[OpenClaw](https://github.com/openclaw/openclaw). Gebündelte
Drittanbieter-Inhalte sind in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) ausgewiesen.

Community-Mitwirkende werden in
[`CONTRIBUTORS.md`](CONTRIBUTORS.md) gewürdigt, einschließlich
release-spezifischer Attributionshinweise für squash-gemergte oder
wiedergespielte Arbeit.

---

## Mitwirkende

Dank an alle, die zu OpenSquilla beitragen.

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=opensquilla/opensquilla&max=100&columns=10" alt="OpenSquilla contributors" />
  </a>
</p>

---

## Mitwirken

Beiträge jeder Art sind willkommen — Fehlerberichte, Funktionsideen,
Dokumentation, neue Provider- oder Kanal-Adapter, Skills und Arbeit an
der Kern-Runtime. Siehe [`CONTRIBUTING.md`](CONTRIBUTING.md) und
eröffne dann ein Issue oder einen Pull Request auf
[GitHub](https://github.com/opensquilla/opensquilla).

[Verhaltenskodex](CODE_OF_CONDUCT.md) · [Sicherheit](SECURITY.md) ·
[Support](SUPPORT.md) · [Lizenz](LICENSE) (Apache-2.0)
