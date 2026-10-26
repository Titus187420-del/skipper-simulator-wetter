# Wetter für den Skipper Simulator

Eigene Wetter-Pipeline des Spiels (getrennt von der BinnenSkipper-Pipeline). Sie holt die Vorhersage des
Deutschen Wetterdienstes und schreibt je Revier eine kleine Datei, die das Spiel („Live-Wetter“, `game/src/livewetter.js`) und
die App (Reiter „Fahren“, `src/spiel/liveWetter.ts`) lesen.

## Was es macht
- Quelle: DWD Open Data, Modelle **ICON-D2** (2 km, 48 Stunden; Bayern, Bodensee, Gardasee) und **ICON-EU** (7 km, 72 Stunden; Kroatien,
  Slowenien, Montenegro — außerhalb des D2-Gebiets). `konfig/reviere.json` legt je Revier einen Punkt fest; `"modell": "auto"` wählt selbst.
- Nimmt immer den neuesten **vollständigen** Modelllauf und schneidet je Revier den nächsten Gitterpunkt heraus.
- Felder je Stunde: Wind 10 m (km/h), Richtung woher (Grad), Böen (km/h), Niederschlag der Stunde (mm), Gesamtbedeckung (%),
  Wettercode (WMO ww), Sichtweite (m), Temperatur 2 m (°C).

## Lokal starten
```
cd SkipperSimulator-Wetter
.venv/bin/python wetter/baue_wetter.py            # alle Reviere, rund 2–4 Minuten
.venv/bin/python wetter/baue_wetter.py --reviere chiemsee,split
```
Mit `--zwischenspeicher <ordner>` werden die Downloads beim Entwickeln zwischengespeichert (rund 1 GB; danach löschen).

## Ausgabe (`ausgabe/`)
`<revier>.json`:
```
{ "revier": "chiemsee", "modell": "ICON-D2", "lauf": "2026-10-25T15:00:00Z", "erstellt": "…Z", "punkt": [47.87, 12.45],
  "quelle": "Deutscher Wetterdienst (ICON), bearbeitet",
  "stunden": [ { "t": "2026-10-25T16:00:00Z", "wind": 9.3, "aus": 238, "boeen": 22.1, "regen": 1.7, "wolken": 100, "code": 61, "sicht": 16993, "temp": 12.3 }, … ] }
```
dazu `index.json` mit Lauf und Modell je Revier. Rund 10 KB je Revier, zusammen etwa 200 KB.

## Quellenvermerk und Hinweise
- „Wetterdaten: Deutscher Wetterdienst (ICON), vom Spiel bearbeitet“ (GeoNutzV § 3: Quelle und Hinweis auf Bearbeitung).
- Im Spiel und in der App: „Spielwetter — keine Wetterberatung und kein Warndienst.“
- Abrufe ohne persönliche Daten (User-Agent `skipper-simulator-wetter/1.0`).

## Für den echten Betrieb fehlt noch (nur nach Freigabe)
1. Ein GitHub-Projekt für diesen Ordner; der Ablauf `.github/workflows/wetter.yml` läuft dann alle 3 Stunden (Vorschlag, heute nirgends eingerichtet).
2. Ziel der Dateien: zunächst der Webspace (huggie.de), nach dem Umzug Cloudflare R2. Das Hochlade-Skript und die geheimen Zugangsdaten
   (GitHub „Secrets“) fehlen bewusst.
3. Danach die Adresse im Spiel (`LIVE_ORDNER` in `game/src/livewetter.js`) und in der App (`src/spiel/liveWetter.ts`) eintragen.
