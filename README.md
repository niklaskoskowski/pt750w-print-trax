# pt750w-print-trax

Netzwerk-Druckbrücke für den **Brother PT-P750W**. Läuft auf einem Raspberry Pi im selben WLAN wie
der Drucker, nimmt Druckaufträge per HTTPS-API entgegen (erreichbar z. B. über Cloudflare Tunnel)
und schickt sie als Brother-Raster direkt an den Drucker (raw TCP, Port 9100).

Gedacht als Gegenstück zum Label-Druck in **inventory-management** (Settings → Printer), aber
generisch: jedes PNG/JPG, Text-Etiketten, Web-UI, CLI.

```text
Browser ──► inventory (Webhosting) ──HTTPS + Token──► Cloudflare ──► cloudflared ──► Bridge (Pi) ──TCP 9100──► PT-P750W
                 api.php printer.print                                               :8750
```

- **Kein Treiber, kein CUPS.** Python 3 + Pillow, Docker-Image für arm64/armhf/amd64.
- **Tape-Erkennung:** fragt vor jedem Druck den Status ab (Tape-Breite, Fehler wie *Abdeckung offen*,
  *kein Tape*) und skaliert das Etikett passend – erst über Port 9100, und weil der PT-P750W das per
  WLAN oft ignoriert, zusätzlich per **SNMP** (Brother-OID, Community `public`). Kommt auf keinem
  Weg ein Status, wird für das erwartete Tape des Auftrags bzw. `PTB_DEFAULT_TAPE_MM` gedruckt.
- **Exakte Größe:** Etiketten werden in mm übergeben und mit 180 dpi 1:1 gedruckt; zu hohe Etiketten
  werden auf den Druckbereich des Tapes verkleinert (mit Warnung).
- **Warteschlange:** ein Auftrag nach dem anderen, parallele Requests warten.
- **Verlauf** der letzten Aufträge mit Rastervorschau (genau das, was der Druckkopf bekommt).

## Tape & Druckbereich (180 dpi, 128 Pins)

| Tape | Druckbereich | 14-mm-Etikett aus inventory |
|---|---|---|
| 24 mm | 18,1 mm | 1:1 |
| 18 mm | 15,8 mm | 1:1 |
| 12 mm | 9,9 mm | auf ~70 % verkleinert |
| 9 / 6 / 3,5 mm | 7,1 / 4,5 / 3,4 mm | stark verkleinert |

Für die Inventory-Labels (14 mm hoch) also **18 oder 24 mm TZe**.

---

## Einrichtung auf dem Raspberry Pi

### 1. Drucker ins WLAN

PT-P750W im **Infrastruktur-Modus** ins WLAN bringen (WPS-Taste am Router oder *Printer Setting
Tool*), dann im Router eine **feste DHCP-Adresse** vergeben. Prüfen vom Pi aus:

```bash
nc -vz 192.168.1.50 9100
```

Tipp: Im *Printer Setting Tool* die **automatische Abschaltung** am Netzteil deaktivieren, sonst ist der
Drucker nach einer Weile nicht mehr erreichbar.

### 2. Bridge starten

```bash
mkdir -p ~/docker && cd ~/docker
git clone https://github.com/niklaskoskowski/pt750w-print-trax.git
cd pt750w-print-trax
cp .env.example .env
sed -i "s|^PTB_PRINTER_HOST=.*|PTB_PRINTER_HOST=192.168.1.50|" .env
sed -i "s|^PTB_TOKEN=.*|PTB_TOKEN=$(openssl rand -hex 32)|" .env
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Web-UI: `http://<pi-ip>:8750` – Token aus `.env` eintragen (`grep PTB_TOKEN .env`).

Test ohne Browser:

```bash
TOKEN=$(grep ^PTB_TOKEN .env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8750/api/status | python3 -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"text":"Hallo\nPT-P750W"}' http://localhost:8750/api/print/text
```

### 3. Von außen erreichbar machen (Cloudflare Tunnel)

**Variante A – vorhandener cloudflared auf dem Pi:** im Cloudflare-Dashboard (Zero Trust → Networks →
Tunnels → *dein Tunnel* → Public Hostname) einen Hostnamen anlegen, z. B. `print.example.com`, Service
`http://localhost:8750` (cloudflared nativ / `network_mode: host`) bzw. `http://<pi-ip>:8750`
(cloudflared in einem anderen Docker-Netz).

**Variante B – eigener Tunnel aus diesem Compose-Stack:** Tunnel im Dashboard anlegen, Token in `.env`
als `CLOUDFLARE_TUNNEL_TOKEN` eintragen, Public Hostname auf `http://bridge:8750` zeigen lassen:

```bash
docker compose --profile tunnel up -d
```

Der Origin ist **HTTP** – TLS endet bei Cloudflare.

**Empfohlen: Cloudflare Access davor.** Zero Trust → Access → Applications → Self-hosted für
`print.example.com`, Policy mit Aktion **Service Auth** und einem **Service Token** (Access → Service
Auth → Service Tokens). Client-ID und Secret kommen in inventory unter Settings → Printer. Damit erreicht
niemand die Bridge, der nicht beides hat: Access-Service-Token *und* Bridge-Token.
(Für die Web-UI im Browser zusätzlich eine normale Allow-Policy mit deiner E-Mail.)

### 4. inventory-management verbinden

Settings → **Printer**: *Enable* einschalten, Bridge-URL (`https://print.example.com`), Token,
optional Access-Service-Token → **Save settings** → **Test connection**. Danach gibt es im Label-Drawer
und unter Settings → Labels den Button **Send to printer**.

---

## API

Auth: `Authorization: Bearer <token>` oder `X-Api-Key: <token>`.

| Methode | Pfad | |
|---|---|---|
| GET | `/health` | ohne Auth |
| GET | `/api/status` | fragt den Drucker live ab; `?cached=1` liefert den letzten Stand ohne Verbindung |
| GET | `/api/jobs` | letzte Aufträge |
| GET | `/api/jobs/<id>/preview.png` | Rastervorschau eines Auftrags |
| POST | `/api/print` | Bild drucken: JSON `{"image":"<base64>", …}` **oder** Bild als Body + Optionen als Query |
| POST | `/api/print/text` | Text-Etikett: JSON `{"text":"Zeile 1\nZeile 2","align":"center", …}` |
| POST | `/api/preview` | wie `/api/print`, rendert nur (Antwort enthält `preview` als PNG-Data-URL) |
| POST | `/api/batches` | Batch starten → `{"batchId": "…"}` |
| POST | `/api/batches/<id>/labels` | ein Etikett hinzufügen: JSON `{"image":"<base64>","widthMm":30,"heightMm":14,"index":0,"name":"…"}` |
| POST | `/api/batches/<id>/print` | alle Etiketten als **ein** Job: JSON `{"orientation":"along\|across","cut":"half","copies":1,"dryRun":false, …}` |
| DELETE | `/api/batches/<id>` | Batch verwerfen (sonst nach 30 min automatisch) |

**Batch:** eine Seite pro Etikett in einem Job – mit `cut: half` halbgeschnitten zwischen den
Etiketten und einmal voll geschnitten am Ende, also ein durchgehender Streifen ohne Vorlauf-Verschnitt
pro Etikett. Die Bilder werden erst beim Drucken für das eingelegte Tape gerendert. `orientation`:
`along` = lange Seite längs zum Band (so groß wie möglich), `across` = um 90° gedreht, lange Seite quer
zum Band (kleiner, kürzerer Streifen). `dryRun` liefert die Vorschau des ganzen Streifens mit den
Schnittlinien.

Optionen (JSON-Felder bzw. Query-Parameter, alle optional):

| Feld | Werte | Standard |
|---|---|---|
| `widthMm`, `heightMm` | physische Größe des Bildes | ohne: Tape füllen |
| `fit` | `exact` (1:1) · `fill` (Tape-Höhe füllen) | `exact` |
| `rotate` | `auto` (lange Seite längs zum Band) · `across` (lange Seite quer, kleiner) · `0` · `90` · `180` · `270` | `auto` |
| `copies` | 1…`PTB_MAX_COPIES` | 1 |
| `cut` | `each` · `half` · `none` | `PTB_CUT` |
| `chain` | Kettendruck (kein Vorschub/Schnitt nach dem letzten Etikett) | `PTB_CHAIN` |
| `marginMm` | Vorschub-Rand je Etikett | `PTB_MARGIN_MM` |
| `tapeMm` | erwartetes Tape; weicht das geladene ab → `409 TAPE_MISMATCH` | – |
| `threshold`, `dither`, `invert` | Schwarzweiß-Umsetzung | 128, aus, aus |
| `highRes` | 180 × 360 dpi (doppelte Auflösung längs zum Band; nur Profile `compat`/`standard`) | `PTB_HIGH_RES`, **an** |
| `profile` | Befehlssatz: `standard` · `minimal` · `compat` · `plain` · `ptouch` – siehe *Befehlssatz finden* | `PTB_PROFILE` |
| `jobName`, `source` | Anzeige im Verlauf | – |
| `dryRun` | nur rendern | aus |

Antwort:

```json
{"ok": true,
 "job": {"id": "…", "state": "printed", "tapeMm": 18, "tapeSource": "printer", "lengthMm": 30.0,
         "heightMm": 14.0, "scalePct": 100, "copies": 1, "warnings": []},
 "preview": "data:image/png;base64,…",
 "printer": {"model": "PT-P750W", "tapeMm": 18, "errors": []}}
```

`state`: `printed` = Drucker hat den Abschluss gemeldet · `sent` = gesendet, Drucker meldet keinen Status.

Fehler: `{"ok": false, "error": {"code": "…", "message": "…"}}` – `UNAUTHORIZED` 401, `BAD_REQUEST` 400,
`BAD_IMAGE` 422, `TAPE_MISMATCH` / `PRINTER_ERROR` / `NO_MEDIA` 409, `OFFLINE` / `BUSY` 503,
`TOO_LARGE` 413.

Beispiel Bild:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: image/png" --data-binary @label.png \
     "https://print.example.com/api/print?widthMm=30&heightMm=14&copies=2"
```

## CLI

Im Container (`docker compose exec bridge …`) oder lokal mit `pip install Pillow`:

```bash
python3 -m ptbridge status
python3 -m ptbridge probe                       # Status roh über Port 9100 und SNMP (Diagnose)
python3 -m ptbridge watch 60                    # jede Statusänderung per SNMP live (Diagnose)
python3 -m ptbridge print label.png --width-mm 30 --height-mm 14 --copies 2
python3 -m ptbridge text "Kabel 12\nXLR 10 m" --cut half
python3 -m ptbridge text "Test" --profile minimal   # minimaler Befehlssatz
python3 -m ptbridge token                       # API-Token anzeigen
python3 -m ptbridge dump label.png -o job.bin --tape 18   # Rohdaten, dann: nc <drucker> 9100 < job.bin
```

## Befehlssatz finden (Drucker geht auf ERROR)

Der PT-P750W quittiert einen Job, den er nicht mag, nur mit blinkender orange/roter Lampe – ohne
Grund. Der Selbsttest druckt pro Befehlssatz ein kurzes Etikett, fragt, ob es herauskam, wartet bei
einem Fehler, bis der Drucker aus- und wieder eingeschaltet ist, und nennt am Ende das passende
`PTB_PROFILE`:

```bash
docker compose exec bridge python3 -m ptbridge selftest
```

**Ergebnis am PT-P750W über WLAN: `compat` druckt** – Standard seit dieser Version. `standard` und
`minimal` (TIFF-Kompression, Print-Info mit Qualitäts-Flag) quittiert er mit ERROR.

| Profil | Inhalt |
|---|---|
| `compat` | **Standard.** `ESC i z` mit Medienprüfung, Auto-Cut/Halbschnitt/Kettendruck/Rand, unkomprimiert |
| `plain` | nur Raster-Modus + Auto-Cut, keine Print-Information, unkomprimiert |
| `ptouch` | byte-genau wie ptouch-print an den P750W (`M 02`, `ESC i a 01`, PackBits als ein Literal) |
| `minimal` | `ESC i z` + Auto-Cut, TIFF/PackBits |
| `standard` | `ESC i z` + Auto-Cut/Halbschnitt/Kettendruck/Rand, TIFF/PackBits – am P750W per WLAN: ERROR |

Halbschnitt, Kettendruck und eigener Rand wirken nur bei `standard` und `compat`.

## Batch: ein Streifen statt einzelner Etiketten

Ein Batch ist ein Job mit einer Seite pro Etikett. Ob der Drucker daraus **einen** Streifen macht
(Halbschnitt zwischen den Etiketten, ein Vollschnitt am Ende) oder jedes Etikett mit eigenem Vorlauf
auswirft, hängt davon ab, wie die Schnitt-/Kettendruck-Einstellungen im Job stehen. Am echten PT-P750W
(12 mm TZe, WLAN) ergab **`noautocut`** einen Streifen – Auto-Cut aus, Halbschnitt an, Einstellungen
einmal am Anfang, ein Vollschnitt am Ende. Das ist der Standard (`PTB_BATCH_MODE=noautocut`). Der Modus lässt sich auch pro Job wählen (`batchMode`, im Inventory unter
Settings → Printer → *Strip mode*). Klappt das nicht:

```bash
docker compose exec bridge python3 -m ptbridge batchtest
```

| Modus | Inhalt |
|---|---|
| `perpage` | Einstellungen auf jeder Seite, Kettendruck an bis zur letzten |
| `chain` | Einstellungen einmal, Kettendruck an – Vorschub + Schnitt nur durch das abschließende `Ctrl-Z` |
| `once` | `ESC i M/K/d` + `M` nur vor der ersten Seite („kein Kettendruck“ gilt dann für alle Seiten) |
| `noautocut` | **Standard.** Wie `once`, Auto-Cut aus – Halbschnitte zwischen den Etiketten, ein Vollschnitt am Ende |
| `legacy` | Einstellungen + „kein Kettendruck“ auf jeder Seite (bis 1.0: jedes Etikett einzeln geschnitten) |

### Etikett nicht mittig zwischen den Schnitten

Das Messer sitzt mechanisch nicht exakt dort, wo der Drucker es annimmt – das Etikett landet dann
ein paar Zehntelmillimeter neben der Mitte seines Abschnitts. `PTB_SHIFT_MM` (bzw. pro Job `shiftMm`,
im Inventory Settings → Printer → *Strip offset*) verschiebt den Druck gegenüber den Schnitten:
negativ = Richtung des Streifenendes, das zuerst herauskommt, positiv = Richtung des Endes, das zuletzt
herauskommt. In 0,1-mm-Schritten nachstellen.

## Ohne Drucker testen

Ein Mock-Drucker beantwortet Statusabfragen und schreibt jede empfangene Seite als PNG – dekodiert
wie der echte Drucker die Daten liest:

```bash
python3 -m ptbridge mock --port 9100 --tape 18 --out ./mock-out
PTB_PRINTER_HOST=127.0.0.1 PTB_TOKEN=test python3 -m ptbridge serve
python3 -m unittest discover -s tests
```

`--silent` simuliert einen Drucker ohne Status-Rückkanal, `--cover-open` einen Fehler.

## Betrieb

```bash
# Update
cd ~/docker/pt750w-print-trax && git pull && docker compose up -d --build

# Logs
docker compose logs -f bridge

# Backup (Konfiguration + Verlauf)
tar -czf pt750w-backup-$(date +%Y%m%d-%H%M%S).tar.gz compose.yaml .env data/
```

## Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| `PermissionError: … /data/previews` | `./data` gehört einem anderen User. Ab dieser Version übernimmt der Container `./data` beim Start selbst (`PUID`/`PGID` in `.env`); alte Version: `sudo chown -R 1000:1000 data`. |
| `OFFLINE … did not answer` | Drucker aus / Auto-Power-Off / andere IP. `nc -vz <ip> 9100` vom Pi. |
| `BUSY` | Ein anderer Auftrag läuft länger als 120 s. |
| `state: sent`, `tapeSource: default` | Kein Status, weder Port 9100 noch SNMP. `docker compose exec bridge python3 -m ptbridge probe` zeigt beide Wege roh. SNMP im Drucker aktivieren (Web-Konfiguration / Printer Setting Tool) oder `PTB_DEFAULT_TAPE_MM` auf das eingelegte Tape setzen bzw. in inventory *Expected tape* wählen. |
| Fehlersuche Protokoll | `watch 60` in einem Terminal, im anderen drucken (Bridge oder Brother-App) – zeigt das Statuspaket roh. Der letzte Job liegt als `data/last-job.bin`, Job-Output enthält `statusBefore`/`statusAfter`. |
| Erster Job druckt, danach nur Vorschub + Schnitt | Verbindung wurde zu früh geschlossen. Seit dieser Version wartet die Bridge, bis der Drucker die Verbindung selbst schließt (`PTB_CLOSE_WAIT`), und vor jedem Job, bis er nicht mehr druckt. Im Log: `connection: printer closed it after …s`. |
| Drucker geht nach dem Job auf ERROR | Drucker aus/an, dann `selftest` (siehe *Befehlssatz finden*). Steht der Drucker noch im Fehler, lehnt die Bridge den nächsten Job ab (`still in an error state`). |
| `state: sent`, `tapeSource: printer` | Normal bei Status per SNMP: Tape erkannt, nur die Druckbestätigung fehlt. |
| `TAPE_MISMATCH` | inventory erwartet ein anderes Tape (Settings → Printer → *Expected tape*). |
| Etikett zu klein | 12-mm-Tape eingelegt – Inventory-Labels brauchen 18/24 mm für 1:1. |
| Cloudflare 502 | Origin falsch: `http://` (nicht https), richtiger Host/Port aus Sicht von cloudflared. |
| Cloudflare 403 vom Inventory-Server | Access-Policy erwartet Service Token → Client-ID/Secret in inventory eintragen. |

## Protokoll-Notizen

Brother *Raster Command Reference PT-E550W/P750W/P710BT*: Invalidate (100 × `00`) → `ESC @` →
`ESC i a 01` (Raster) → je Seite `ESC i z` (Print-Info, Flags `0x86`: Medientyp + Breite gültig),
`ESC i M` (Auto-Cut), `ESC i K` (Halbschnitt / Kettendruck / 360 dpi), `ESC i d` (Rand), `M 00`
(unkomprimiert), Rasterzeilen `G 10 00` + 16 Byte, `FF` zwischen Seiten, `Ctrl-Z` am Ende
(Profil `compat`; die anderen Profile siehe *Befehlssatz finden*). Der Status kommt per SNMP – der
P750W beantwortet `ESC i S` über WLAN nicht. **Nicht** gesendet werden `ESC i A` und `Z`: nicht im P750W-Befehlssatz, ein unbekannter
Befehl schickt den Drucker sofort in ERROR. Status zusätzlich per SNMP
(`1.3.6.1.4.1.2435.3.3.9.1.6.1.0`, gleiches 32-Byte-Paket). Eine Rasterzeile = 16 Byte = 128 Pins quer zum Tape, Bit 7 von Byte 0 = Pin 0; das Tape liegt
symmetrisch in der Mitte des Kopfes. Code: `ptbridge/protocol.py`, `ptbridge/raster.py`.
