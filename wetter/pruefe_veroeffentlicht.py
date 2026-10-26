#!/usr/bin/env python3
"""Prüft, ob eine hochgeladene Wetterdatei wirklich öffentlich abrufbar ist.

(Übernommen aus BinnenSkipper-Wetter am 25.10.2026; liest beim Skipper Simulator das Feld `erstellt` jeder Reviersdatei.)

⚠️ WOZU DAS DA IST

Ein erfolgreicher SFTP-Upload beweist NICHT, dass die Datei dort liegt, wo die
App sie sucht. Die Zielpfade sind relativ zum Startverzeichnis des SFTP-Zugangs
— und das ist je nach Zugang der Hauptordner, `chiemsee-skipper/` oder
`binnenskipper/`. Passt der Pfad nicht dazu, entsteht ein verschachtelter Ordner:
Der Ablauf bleibt grün, und die App bekommt tagelang veraltete Daten, ohne dass
irgendwo etwas meldet.

Genau das ist dreimal passiert — am 2026-07-16, am 19.07. und am 28.07.

Deshalb ruft dieses Skript nach dem Upload die Adresse ab, die auch die App
abruft, und prüft, ob die Datei WIRKLICH gerade neu geschrieben wurde.

⚠️ ES MÜSSEN ALLE MODELLE GEPRÜFT WERDEN, nicht nur eines.

Bis zum 2026-07-28 prüfte dieser Schritt nur `icon-d2.json`. Das reichte nicht:
Schafft ICON-EU seinen Lauf nicht, überspringt `build_weather.py` das Modell,
`upload_sftp.py` lädt nur hoch was gebaut wurde, und die ALTE icon-eu.json bleibt
auf dem Webspace liegen. Der Lauf wird grün, ICON-D2 ist frisch — und ICON-EU
altert still weiter, bis die App es nach 12 Stunden verwirft und wortlos auf
Bright Sky zurückfällt. Genau so hat sich das auf dem Chiemsee gezeigt.

Deshalb bekommt dieses Skript ALLE Adressen und prüft jede einzeln. Es bricht
nicht beim ersten Fehler ab, sondern nennt am Ende jede betroffene Datei — sonst
verdeckt das erste Problem das zweite.

Aufruf:
    python pruefe_veroeffentlicht.py <adresse> [<adresse> ...] [--minuten N]

Rückgabe: 0 wenn ALLE frisch sind, sonst 1 (mit Begründung auf der Ausgabe).
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

STANDARD_MAX_ALTER_MIN = 30


def pruefe(adresse: str, max_alter: float) -> bool:
    """True, wenn unter dieser Adresse eine frisch geschriebene Datei liegt."""
    try:
        with urllib.request.urlopen(adresse, timeout=30) as antwort:
            daten = json.load(antwort)
    except Exception as fehler:  # noqa: BLE001 — jede Ursache ist hier gleich schlimm
        print(f"    FEHLER: {adresse} ist nicht abrufbar ({fehler})")
        print("    Die Datei liegt also nicht dort, wo die App sie sucht.")
        return False

    # ⚠️ Zwei Dateiformate, zwei Feldnamen.
    #
    # Die Vorhersagen (icon-d2.json, icon-eu.json) tragen den Schreibzeitpunkt
    # in `updated`. Die Messwerte einer Station (messung.json) haben kein
    # `updated` — dort steht in `time` der MESSzeitpunkt, und das ist die Zahl,
    # auf die es ankommt: Eine Datei, die frisch geschrieben wurde, aber eine
    # zwei Stunden alte Messung enthaelt, nuetzt der App nichts.
    #
    # Am 2026-09-01 lief der neue Messwert-Ablauf deshalb rot, obwohl die Datei
    # richtig und frisch auf dem Server lag.
    geschrieben = daten.get("erstellt") or daten.get("updated") or daten.get("time")      # (Skipper Simulator: Feld erstellt)
    if not geschrieben:
        print(f"    FEHLER: {adresse} enthält weder 'updated' noch 'time'")
        return False

    try:
        zeitpunkt = datetime.fromisoformat(str(geschrieben).replace("Z", "+00:00"))
    except ValueError:
        print(f"    FEHLER: unlesbarer Zeitstempel '{geschrieben}'")
        return False

    alter_min = (datetime.now(timezone.utc) - zeitpunkt).total_seconds() / 60
    if alter_min > max_alter:
        print(f"    FEHLER: Unter {adresse} liegt eine ALTE Datei.")
        print(f"    Geschrieben {geschrieben} — das ist {alter_min:.0f} Minuten her.")
        print("    Zwei mögliche Ursachen, beide ernst:")
        print("      1. Der Upload ging woandershin — Zielpfad gegen das")
        print("         Startverzeichnis des SFTP-Zugangs prüfen.")
        print("      2. Dieses Modell wurde gar nicht neu gebaut (kein vollständiger")
        print("         DWD-Lauf gefunden), sodass die alte Datei liegen blieb.")
        print("         Dann steht der Grund weiter oben im Protokoll unter '=== <modell> ==='.")
        return False

    stunden = len(daten.get("hourly", []))
    if stunden:
        print(f"    frisch: geschrieben vor {alter_min:.0f} Minuten, {stunden} Stunden Vorhersage")
    else:
        # Messwertdatei — keine Vorhersagestunden, dafuer der Messwert selbst.
        wind = daten.get("windSpeedMs")
        zusatz = f", {wind} m/s" if wind is not None else ""
        print(f"    frisch: Stand vor {alter_min:.0f} Minuten{zusatz}")
    return True


def main() -> int:
    adressen: list[str] = []
    max_alter = STANDARD_MAX_ALTER_MIN
    rest = list(sys.argv[1:])
    while rest:
        arg = rest.pop(0)
        if arg == "--minuten":
            if not rest:
                print("    FEHLER: --minuten ohne Zahl")
                return 1
            try:
                max_alter = float(rest.pop(0))
            except ValueError:
                print("    FEHLER: --minuten erwartet eine Zahl")
                return 1
        else:
            adressen.append(arg)

    if not adressen:
        print("    FEHLER: keine Adresse angegeben")
        return 1

    # Bewusst KEIN Abbruch beim ersten Fehler: sonst verdeckt ein kaputtes
    # Modell das andere und man repariert zweimal statt einmal.
    fehlend = [a for a in adressen if not pruefe(a, max_alter)]

    if fehlend:
        print(f"    {len(fehlend)} von {len(adressen)} Dateien sind NICHT aktuell:")
        for a in fehlend:
            print(f"      - {a}")
        return 1

    print(f"    Alle {len(adressen)} Dateien sind aktuell.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
