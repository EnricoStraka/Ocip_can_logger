# OCIP CAN Logger

[Deutsch](#deutsch) | [English](#english)

---

## Deutsch

### Projektübersicht

Der **OCIP CAN Logger** ist eine professionelle Python-Anwendung für die Erfassung, Visualisierung, Analyse und Protokollierung von CAN-Daten auf Linux- und Yocto-Systemen. Die Software kombiniert eine moderne **GTK4-Touchoberfläche** mit einer **integrierten Weboberfläche**, sodass CAN-Kommunikation sowohl lokal auf dem Gerät als auch remote im Browser überwacht und gesteuert werden kann.

Das Projekt eignet sich besonders für **Embedded-Systeme**, **Diagnoseplätze**, **Testaufbauten**, **Service-Tools** und **Industrie-/Automotive-Anwendungen**, bei denen eine robuste, direkt bedienbare und visuell moderne CAN-Lösung benötigt wird.

![OCIP CAN Logger](ocipcanlogger.png)

### Highlights

- Professioneller **SocketCAN Logger** auf Basis von `python-can`
- Moderne **GTK4-Oberfläche** für Touchscreens, Kiosk-Systeme und Panel-PCs
- **Integriertes Live-Webdashboard** ohne externe Frameworks
- **CAN-Frames senden** direkt aus der GTK-Oberfläche oder aus dem Browser
- **Eingebaute Hex-Tastatur** für Yocto-/Touch-Systeme ohne externe Bildschirmtastatur
- Gleichzeitiges Logging in mehreren Formaten:
  - `can_logger.log`
  - `can_logger.csv`
  - `can_logger.asc`
  - `can_logger_stats.json`
- **Live-Statistiken** zu RX, TX, Fehlerframes, Datenrate, Uptime und Top-IDs
- **CAN-Interface-Rekonfiguration** direkt aus UI und Web-App
- **Log-Rotation** mit Größenlimit und Backups
- **CAN-Filter-Unterstützung**
- Geeignet für **Vollbild-/Kiosk-Betrieb** und **Windowed-Modus**

### Funktionsumfang

Der Logger liest CAN-Daten zyklisch über SocketCAN ein, visualisiert die Daten live und speichert sie parallel in mehreren Formaten. Zusätzlich können eigene CAN-Nachrichten aktiv gesendet werden.

Die Anwendung bietet dafür:

- Live-Tabelle empfangener und gesendeter Frames
- Anzeige des letzten Frames
- Statistiken über Buslast und Aktivität
- Übersicht häufiger CAN-IDs
- Konfiguration von Kanal und Bitrate
- Exportierbare und analysierbare Logdateien
- Browserzugriff auf Status, Daten und Sende-/Konfigurationsfunktionen

### Architektur

Die Software ist in mehrere funktionale Bereiche gegliedert:

#### 1. CAN-Kommunikation
Ein dedizierter Worker-Thread übernimmt das Öffnen des CAN-Busses, das Empfangen von Frames sowie das Senden eigener Telegramme.

#### 2. Logging-Schicht
Jede relevante CAN-Nachricht wird in mehreren Zielformaten gespeichert. Dadurch kann die gleiche Datenbasis sowohl für Debugging als auch für spätere Auswertung verwendet werden.

#### 3. GTK4-Benutzeroberfläche
Die lokale Oberfläche ist auf Touch-Bedienung optimiert und zeigt Live-Werte, Status, letzte Frames und Sende-/Konfigurationsfunktionen übersichtlich an.

#### 4. Eingebettete Webanwendung
Ein integrierter HTTP-Server liefert ein responsives Live-Dashboard. Darüber kann das System remote beobachtet und teilweise gesteuert werden.

### Repository-Inhalt

- `ocip_can_logger.py` – Hauptanwendung mit CAN-Logger, GTK4-Oberfläche und Webserver
- `ocipcanlogger.png` – Hauptgrafik / Projektvorschau
- `ocip_can_logger_1.png` – Screenshot der Anwendung
- `ocip_can_logger_2.jpeg` – zusätzliche Ansicht / Screenshot
- `README.md` – Projektdokumentation
- `LICENSE` – Lizenzdatei

### Voraussetzungen

#### Systempakete
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-cairo python3-gi-cairo
```

#### Python-Abhängigkeit
```bash
pip install python-can
```

### Schnellstart

#### Standardstart
```bash
python3 ocip_can_logger.py
```

#### Mit Interface-Konfiguration
```bash
python3 ocip_can_logger.py --channel can0 --bitrate 250000 --configure-can
```

#### Im Fenstermodus
```bash
python3 ocip_can_logger.py --windowed --log-dir /tmp/canlogs
```

#### Mit Weboberfläche
```bash
python3 ocip_can_logger.py --web-host 0.0.0.0 --web-port 8080
```

Aufruf im Browser:

```text
http://<geraete-ip>:8080
```

### Wichtige Kommandozeilenoptionen

- `--channel` – CAN-Kanal, z. B. `can0`
- `--interface` – Interface-Typ, Standard: `socketcan`
- `--bitrate` – Bitrate für optionales Interface-Setup
- `--configure-can` – konfiguriert das Interface beim Start per `ip link`
- `--restart-ms` – Restart-Zeit für SocketCAN
- `--log-dir` – Zielordner für Logdateien
- `--max-bytes` – maximale Dateigröße pro Logdatei
- `--backups` – Anzahl rotierter Backups
- `--filter` – CAN-Filter, z. B. `123:7FF,1CEFFF24:1FFFFFFF`
- `--windowed` – startet die Anwendung im Fenster
- `--web-host` – Host-Adresse der Webanwendung
- `--web-port` – Port der Webanwendung
- `--no-web` – deaktiviert die Weboberfläche

### Logformate

#### `can_logger.log`
Textbasiertes, `candump`-ähnliches Format für schnelles Lesen und Debugging.

#### `can_logger.csv`
Tabellarisches Format für Excel, LibreOffice, Python-Pandas oder andere Analysewerkzeuge.

#### `can_logger.asc`
ASC-ähnliches Format für typische Automotive-Analyse-Workflows.

#### `can_logger_stats.json`
Strukturierte Status- und Statistikdaten für Monitoring, Automatisierung oder externe Auswertung.

### Vorteile für Yocto / Embedded

Dieses Projekt ist besonders für Embedded-Umgebungen interessant, weil:

- keine externe Bildschirmtastatur erforderlich ist
- die Bedienung für Touch optimiert wurde
- die Weboberfläche ohne zusätzliche Web-Frameworks auskommt
- Logging, Visualisierung und Sendefunktion in einer Anwendung kombiniert sind
- die Software für Fullscreen-/Kiosk-Betrieb geeignet ist

### Typische Einsatzbereiche

- Embedded-Linux-Diagnose
- Service- und Testsysteme
- Labor- und Integrationsumgebungen
- Automotive-Prototyping
- Maschinen- und Anlagenkommunikation
- Visualisierung von Buskommunikation auf Touchpanels

### Screenshots

#### Hauptansicht
![Hauptansicht](ocip_can_logger_1.png)

#### Weitere Ansicht
![Weitere Ansicht](ocip_can_logger_2.jpeg)

### Technologiestack

- **Python 3**
- **GTK4 / PyGObject**
- **python-can**
- **SocketCAN**
- **integrierter Python-HTTP-Server**

### Deutsche Kurzbeschreibung für GitHub About

**Variante professionell:**

> Professioneller GTK4 SocketCAN Logger mit Touch-UI, CAN-Sendefunktion, Live-Statistiken und integrierter Weboberfläche für Linux- und Yocto-Systeme.

**Variante kurz:**

> Moderner CAN-Logger mit GTK4-Touchoberfläche, Webdashboard und SocketCAN-Unterstützung für Linux/Yocto.

### Empfohlene GitHub Topics

```text
can
socketcan
python
gtk4
yocto
embedded-linux
can-bus
logger
diagnostics
dashboard
automotive
linux
```
### Entwicklung unterstützen

Wenn dir das Projekt gefällt oder es dir hilft, kannst du die Weiterentwicklung freiwillig per PayPal unterstützen:

[Per PayPal unterstützen] paypal.me/EnricoStrakaOCIPms
---

## English

### Project Overview

**OCIP CAN Logger** is a professional Python application for capturing, visualizing, analyzing, and logging CAN data on Linux and Yocto systems. The software combines a modern **GTK4 touch user interface** with an **integrated web dashboard**, allowing CAN communication to be monitored and controlled both locally on the device and remotely from a browser.

The project is especially suitable for **embedded systems**, **diagnostic stations**, **test benches**, **service tools**, and **industrial / automotive environments** where a robust, directly operable, and visually modern CAN solution is required.

![OCIP CAN Logger](ocipcanlogger.png)

### Highlights

- Professional **SocketCAN logger** based on `python-can`
- Modern **GTK4 interface** for touchscreens, kiosk systems, and panel PCs
- **Integrated live web dashboard** without external web frameworks
- **CAN frame transmission** directly from the GTK UI or the browser
- Built-in **hex keyboard** for Yocto/touch systems without an external on-screen keyboard
- Simultaneous multi-format logging:
  - `can_logger.log`
  - `can_logger.csv`
  - `can_logger.asc`
  - `can_logger_stats.json`
- **Live statistics** for RX, TX, error frames, data rate, uptime, and top CAN IDs
- **CAN interface reconfiguration** directly from the UI and web app
- **Log rotation** with size limits and backup files
- **CAN filter support**
- Suitable for both **fullscreen kiosk operation** and **windowed mode**

### Feature Set

The logger continuously reads CAN traffic via SocketCAN, visualizes the data live, and stores it in multiple output formats in parallel. In addition, custom CAN messages can be transmitted actively.

The application provides:

- Live table of received and transmitted frames
- Last-frame display
- Bus activity and performance statistics
- Overview of frequently occurring CAN IDs
- Channel and bitrate configuration
- Exportable and analysis-friendly log files
- Browser-based access to status, live data, sending, and configuration functions

### Architecture

The software is structured into several functional layers:

#### 1. CAN Communication
A dedicated worker thread handles CAN bus opening, frame reception, and active CAN frame transmission.

#### 2. Logging Layer
Each relevant CAN message is written into multiple output formats. This makes the same dataset suitable for both quick debugging and later analysis.

#### 3. GTK4 User Interface
The local interface is optimized for touch operation and presents live values, status information, recent frames, and transmission/configuration functions in a clear layout.

#### 4. Embedded Web Application
An integrated HTTP server provides a responsive live dashboard, enabling remote monitoring and partial remote control.

### Repository Contents

- `ocip_can_logger.py` – main application containing CAN logger, GTK4 UI, and web server
- `ocipcanlogger.png` – main preview image
- `ocip_can_logger_1.png` – application screenshot
- `ocip_can_logger_2.jpeg` – additional screenshot / view
- `README.md` – project documentation
- `LICENSE` – license file

### Requirements

#### System packages
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 python3-cairo python3-gi-cairo
```

#### Python dependency
```bash
pip install python-can
```

### Quick Start

#### Standard startup
```bash
python3 ocip_can_logger.py
```

#### With interface configuration
```bash
python3 ocip_can_logger.py --channel can0 --bitrate 250000 --configure-can
```

#### Windowed mode
```bash
python3 ocip_can_logger.py --windowed --log-dir /tmp/canlogs
```

#### With web dashboard
```bash
python3 ocip_can_logger.py --web-host 0.0.0.0 --web-port 8080
```

Browser access:

```text
http://<device-ip>:8080
```

### Important Command Line Options

- `--channel` – CAN channel, e.g. `can0`
- `--interface` – interface type, default: `socketcan`
- `--bitrate` – bitrate for optional interface setup
- `--configure-can` – configures the CAN interface on startup using `ip link`
- `--restart-ms` – SocketCAN restart timing
- `--log-dir` – output directory for log files
- `--max-bytes` – maximum log file size before rotation
- `--backups` – number of rotated backup files
- `--filter` – CAN filter, e.g. `123:7FF,1CEFFF24:1FFFFFFF`
- `--windowed` – starts the application in windowed mode
- `--web-host` – web application host
- `--web-port` – web application port
- `--no-web` – disables the web dashboard

### Log Formats

#### `can_logger.log`
Text-based `candump`-like output for quick reading and debugging.

#### `can_logger.csv`
Tabular output format for Excel, LibreOffice, Python/Pandas, or other analysis workflows.

#### `can_logger.asc`
ASC-like format for common automotive analysis workflows.

#### `can_logger_stats.json`
Structured status and statistical data for monitoring, automation, or external processing.

### Benefits for Yocto / Embedded

This project is especially well suited for embedded environments because:

- no external on-screen keyboard is required
- the interface is optimized for touch operation
- the web dashboard works without external web frameworks
- logging, visualization, and frame transmission are combined in one application
- the software is suitable for fullscreen / kiosk deployments

### Typical Use Cases

- Embedded Linux diagnostics
- Service and test systems
- Lab and integration environments
- Automotive prototyping
- Machine and plant communication monitoring
- Touch panel visualization of bus communication

### Screenshots

#### Main View
![Main View](ocip_can_logger_1.png)

#### Additional View
![Additional View](ocip_can_logger_2.jpeg)

### Technology Stack

- **Python 3**
- **GTK4 / PyGObject**
- **python-can**
- **SocketCAN**
- **integrated Python HTTP server**

### English GitHub About Description

**Professional version:**

> Professional GTK4 SocketCAN logger with touch UI, CAN frame transmission, live statistics, and integrated web dashboard for Linux and Yocto systems.

**Short version:**

> Modern GTK4 SocketCAN logger with touch UI, live web dashboard, and CAN frame transmission for Linux and Yocto systems.

### Recommended GitHub Topics

```text
can
socketcan
python
gtk4
yocto
embedded-linux
can-bus
logger
diagnostics
dashboard
automotive
linux
```
### Support Development

If you find this project useful and want to support further development, you can donate via PayPal:

[Donate via PayPal] paypal.me/EnricoStrakaOCIPms
---

## License

This repository contains a `LICENSE` file.
