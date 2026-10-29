#!/usr/bin/env python3
"""Live-Wetter fuer den Skipper Simulator: eine kleine JSON-Datei je Revier.

Datenquelle: Deutscher Wetterdienst (DWD) Open Data, https://opendata.dwd.de/weather/nwp/
  * ICON-D2  (Deutschland und Umgebung, ca. 2,2 km, stuendlich bis +48 h)
  * ICON-EU  (Europa, ca. 6,5 km, stuendlich bis +78 h; wir lesen bis +72 h)

Ablauf:
  1. Reviere aus konfig/reviere.json lesen.
  2. Je Modell den neuesten VOLLSTAENDIG veroeffentlichten Lauf suchen (die Datei
     der letzten Vorhersagestunde muss fuer jedes benoetigte Feld da sein).
  3. Pruefen, welche Punkte im ICON-D2-Gebiet liegen (ausserhalb ist das Gitter
     leer, am Rand ist es unzuverlaessig); alle anderen nimmt ICON-EU.
  4. Jedes Feld je Stunde EINMAL laden und daraus alle Punkte des Modells lesen
     (naechster Gitterpunkt). Das spart Abrufe gegenueber "je Revier ein Lauf".
  5. ausgabe/<revier>.json und ausgabe/index.json schreiben.

Dateinamen unterscheiden sich je Modell (Falle aus der BinnenSkipper-Pipeline):
  ICON-D2: icon-d2_germany_regular-lat-lon_single-level_<LAUF>_<FFF>_2d_<param>.grib2.bz2
           (Parameter klein, mit "2d_")
  ICON-EU: icon-eu_europe_regular-lat-lon_single-level_<LAUF>_<FFF>_<PARAM>.grib2.bz2
           (Parameter GROSS, ohne "2d_")
  Der Ordner heisst immer klein: .../grib/<HH>/<param>/

Lizenz der Daten: GeoNutzV, Quellenvermerk "Deutscher Wetterdienst (ICON), bearbeitet".
"""

from __future__ import annotations

import argparse
import bz2
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests
import xarray as xr

WURZEL = Path(__file__).resolve().parent.parent
BASIS_URL = "https://opendata.dwd.de/weather/nwp"
QUELLE = "Deutscher Wetterdienst (ICON), bearbeitet"

# Abstand (Grad) zum Rand des ICON-D2-Gebiets, den ein Punkt mindestens haben
# muss, damit er aus ICON-D2 gelesen wird. Am Rand liegt die Randzone des
# Modells; ein Punkt direkt dort (z. B. Split) ist in ICON-EU besser aufgehoben.
D2_RAND_GRAD = 0.2

# Kein Name, keine Mailadresse: nur Werkzeugname.
KOPF = {"User-Agent": "skipper-simulator-wetter/1.0"}
HTTP_TIMEOUT = 60
WIEDERHOLUNGEN = 3

log = logging.getLogger("baue_wetter")


# ---------------------------------------------------------------------------
# Felder und Modelle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Feld:
    ordner: str       # Ordnername beim DWD, klein
    d2: str           # Dateiteil bei ICON-D2
    eu: str           # Dateiteil bei ICON-EU
    pflicht: bool = False
    ebene: Optional[int] = None   # Druckflaeche in hPa (pressure-level), sonst Bodenfeld (single-level)


FELDER: List[Feld] = [
    Feld("u_10m", "2d_u_10m", "U_10M", pflicht=True),
    Feld("v_10m", "2d_v_10m", "V_10M", pflicht=True),
    Feld("vmax_10m", "2d_vmax_10m", "VMAX_10M"),
    Feld("tot_prec", "2d_tot_prec", "TOT_PREC"),
    Feld("clct", "2d_clct", "CLCT"),
    Feld("ww", "2d_ww", "WW"),
    Feld("vis", "2d_vis", "VIS"),
    Feld("t_2m", "2d_t_2m", "T_2M"),
    # Wind in 850 hPa (rund 1,5 km Hoehe): danach ziehen die Wolken im Spiel (29.10.2026). Kein Pflichtfeld — fehlt er, gilt der Bodenwind.
    Feld("u", "u", "U", ebene=850),
    Feld("v", "v", "V", ebene=850),
]


@dataclass(frozen=True)
class Modell:
    kurz: str            # "icon-d2" (Ordner beim DWD)
    name: str            # "ICON-D2" (Ausgabe)
    gitter: str          # "germany" / "europe"
    bis_stunde: int      # letzte Vorhersagestunde, die wir lesen
    laeufe: Tuple[int, ...]   # Uhrzeiten (UTC) der moeglichen Laeufe
    rueckblick: int      # so viele Laeufe hoechstens zurueck probieren


ICON_D2 = Modell("icon-d2", "ICON-D2", "germany", 48, (0, 3, 6, 9, 12, 15, 18, 21), 4)
# ICON-EU: nur 00/06/12/18 Uhr reichen bis +120 h; die Zwischenlaeufe nur bis
# +30 h. Die Pruefung auf +72 h sortiert sie von selbst aus. Acht Kandidaten
# decken vier Hauptlaeufe (einen Tag) ab; der DWD raeumt danach weg.
ICON_EU = Modell("icon-eu", "ICON-EU", "europe", 72, (0, 3, 6, 9, 12, 15, 18, 21), 8)
MODELLE = {"icon-d2": ICON_D2, "icon-eu": ICON_EU}


def url_zu(modell: Modell, lauf: dt.datetime, feld: Feld, stunde: int) -> str:
    teil = feld.d2 if modell is ICON_D2 else feld.eu
    if feld.ebene:
        name = (f"{modell.kurz}_{modell.gitter}_regular-lat-lon_pressure-level_"
                f"{lauf:%Y%m%d%H}_{stunde:03d}_{feld.ebene}_{teil}.grib2.bz2")
    else:
        name = (f"{modell.kurz}_{modell.gitter}_regular-lat-lon_single-level_"
                f"{lauf:%Y%m%d%H}_{stunde:03d}_{teil}.grib2.bz2")
    return f"{BASIS_URL}/{modell.kurz}/grib/{lauf:%H}/{feld.ordner}/{name}"


# ---------------------------------------------------------------------------
# Netz
# ---------------------------------------------------------------------------

_sitzung: Optional[requests.Session] = None


def sitzung() -> requests.Session:
    global _sitzung
    if _sitzung is None:
        _sitzung = requests.Session()
        _sitzung.headers.update(KOPF)
    return _sitzung


def url_da(url: str) -> bool:
    try:
        r = sitzung().head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException as exc:
        log.debug("HEAD %s: %s", url, exc)
        return False


def lauf_vollstaendig(modell: Modell, lauf: dt.datetime) -> bool:
    """Alle Felder muessen ihre LETZTE Stunde schon haben (der DWD schreibt die
    Stunden der Reihe nach; die letzte beweist, dass der Lauf fertig ist)."""
    return all(url_da(url_zu(modell, lauf, f, modell.bis_stunde)) for f in FELDER if f.pflicht) \
        and all(url_da(url_zu(modell, lauf, f, modell.bis_stunde)) for f in FELDER if not f.pflicht)


def neuester_lauf(modell: Modell, jetzt: dt.datetime) -> Optional[dt.datetime]:
    kandidaten: List[dt.datetime] = []
    zeiger = jetzt.replace(minute=0, second=0, microsecond=0)
    for _ in range(36):
        if zeiger.hour in modell.laeufe:
            kandidaten.append(zeiger)
            if len(kandidaten) >= modell.rueckblick:
                break
        zeiger -= dt.timedelta(hours=1)
    for lauf in kandidaten:
        if lauf_vollstaendig(modell, lauf):
            log.info("[%s] Lauf %s UTC ist vollstaendig (%.1f h alt)", modell.name,
                     f"{lauf:%Y-%m-%d %H}:00", (jetzt - lauf).total_seconds() / 3600)
            return lauf
        log.info("[%s] Lauf %s UTC noch nicht vollstaendig, probiere aelteren",
                 modell.name, f"{lauf:%H}:00")
    log.error("[%s] kein vollstaendiger Lauf gefunden", modell.name)
    return None


def lade(url: str, zwischenspeicher: Optional[Path]) -> Optional[bytes]:
    """bz2-Datei laden und entpacken. None bei 404 oder dauerhaftem Fehler."""
    datei = None
    if zwischenspeicher is not None:
        datei = zwischenspeicher / hashlib.sha1(url.encode()).hexdigest()
        if datei.exists():
            return bz2.decompress(datei.read_bytes())
    for versuch in range(1, WIEDERHOLUNGEN + 1):
        try:
            r = sitzung().get(url, timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if datei is not None:
                datei.parent.mkdir(parents=True, exist_ok=True)
                datei.write_bytes(r.content)
            return bz2.decompress(r.content)
        except (requests.RequestException, OSError) as exc:
            log.warning("Abruf %d/%d gescheitert (%s): %s", versuch, WIEDERHOLUNGEN,
                        url.rsplit("/", 1)[-1], exc)
            time.sleep(2 * versuch)
    return None


# ---------------------------------------------------------------------------
# GRIB lesen
# ---------------------------------------------------------------------------

Punkt = Tuple[str, float, float]   # (name, lat, lon)


def lese_punkte(grib: bytes, punkte: List[Punkt]) -> Dict[str, Tuple[Optional[float], bool]]:
    """Wert am naechsten Gitterpunkt je Punkt, dazu: ist die Umgebung (D2_RAND_GRAD)
    ganz gefuellt? (Letzteres entscheidet, ob ICON-D2 den Punkt abdeckt.)"""
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as t:
        t.write(grib)
        pfad = t.name
    try:
        ds = xr.open_dataset(pfad, engine="cfgrib", backend_kwargs={"indexpath": ""})
        da = ds[list(ds.data_vars)[0]]
        werte = np.asarray(da.values, dtype=float)
        if werte.ndim == 3:
            # tot_prec (ICON-D2) traegt 4 Viertelstunden je Datei; der erste
            # Schritt ist die volle Stunde.
            werte = werte[0]
        breiten = ds["latitude"].values
        laengen = ds["longitude"].values
        laengen = np.where(laengen > 180.0, laengen - 360.0, laengen)
        schritt = float(abs(breiten[1] - breiten[0]))
        r = int(math.ceil(D2_RAND_GRAD / schritt))
        aus: Dict[str, Tuple[Optional[float], bool]] = {}
        for name, lat, lon in punkte:
            iy = int(np.abs(breiten - lat).argmin())
            ix = int(np.abs(laengen - lon).argmin())
            w = float(werte[iy, ix])
            wert = w if math.isfinite(w) else None
            fenster_ok = (iy - r >= 0 and iy + r < len(breiten)
                          and ix - r >= 0 and ix + r < len(laengen)
                          and bool(np.isfinite(werte[iy - r:iy + r + 1, ix - r:ix + r + 1]).all()))
            aus[name] = (wert, fenster_ok)
        ds.close()
        return aus
    finally:
        try:
            os.unlink(pfad)
        except OSError:
            pass


def aufgabe(args) -> Tuple[str, int, Optional[Dict[str, Tuple[Optional[float], bool]]]]:
    """Ein Prozess: eine Datei laden, lesen. (Prozesse statt Threads: ecCodes
    gilt in Python nicht als zuverlaessig threadsicher.)"""
    url, ordner, stunde, punkte, zwischenspeicher = args
    logging.getLogger("cfgrib").setLevel(logging.ERROR)
    roh = lade(url, Path(zwischenspeicher) if zwischenspeicher else None)
    if roh is None:
        return ordner, stunde, None
    try:
        return ordner, stunde, lese_punkte(roh, punkte)
    except Exception as exc:  # noqa: BLE001 - cfgrib wirft vieles
        log.warning("GRIB nicht lesbar (%s +%dh): %s", ordner, stunde, exc)
        return ordner, stunde, None


def lade_modell(modell: Modell, lauf: dt.datetime, punkte: List[Punkt],
                zwischenspeicher: Optional[str], arbeiter: int
                ) -> Dict[str, Dict[int, Dict[str, Tuple[Optional[float], bool]]]]:
    """-> {feld: {stunde: {punkt: (wert, fenster_ok)}}}"""
    stunden = list(range(0, modell.bis_stunde + 1))
    auftraege = [(url_zu(modell, lauf, f, h), f.ordner, h, punkte, zwischenspeicher)
                 for f in FELDER for h in stunden]
    ergebnis: Dict[str, Dict[int, dict]] = {f.ordner: {} for f in FELDER}
    with cf.ProcessPoolExecutor(max_workers=arbeiter) as pool:
        for ordner, h, aus in pool.map(aufgabe, auftraege, chunksize=2):
            if aus is not None:
                ergebnis[ordner][h] = aus
    for f in FELDER:
        log.info("[%s] %-9s %d/%d Stunden gelesen", modell.name, f.ordner,
                 len(ergebnis[f.ordner]), len(stunden))
    return ergebnis


# ---------------------------------------------------------------------------
# Umrechnung und Ausgabe
# ---------------------------------------------------------------------------

def runde(x: Optional[float], stellen: int = 1) -> Optional[float]:
    if x is None or not math.isfinite(x):
        return None
    return round(x, stellen)


def ganz(x: Optional[float]) -> Optional[int]:
    if x is None or not math.isfinite(x):
        return None
    return int(round(x))


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def stunden_fuer(punkt: str, lauf: dt.datetime, modell: Modell,
                 roh: Dict[str, Dict[int, dict]]) -> List[dict]:
    def w(feld: str, h: int) -> Optional[float]:
        e = roh.get(feld, {}).get(h)
        return None if e is None else e[punkt][0]

    zeilen: List[dict] = []
    for h in range(1, modell.bis_stunde + 1):
        u, v = w("u_10m", h), w("v_10m", h)
        if u is None or v is None:
            continue
        wind_ms = math.hypot(u, v)
        # Meteorologisch: Richtung, AUS der der Wind kommt.
        aus = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
        hu, hv = w("u", h), w("v", h)   # (Wind in 850 hPa, fuer die Wolken)
        gesamt, davor = w("tot_prec", h), w("tot_prec", h - 1)
        regen = None
        if gesamt is not None and davor is not None:
            regen = max(0.0, gesamt - davor)   # kleine negative Rundung abfangen
        zeilen.append({
            "t": iso(lauf + dt.timedelta(hours=h)),
            "wind": runde(wind_ms * 3.6),
            "aus": ganz(aus) % 360 if ganz(aus) is not None else None,
            "temp": runde(None if w("t_2m", h) is None else w("t_2m", h) - 273.15),
            "boeen": runde(None if w("vmax_10m", h) is None else w("vmax_10m", h) * 3.6),
            "regen": runde(regen),
            "wolken": ganz(w("clct", h)),
            "code": ganz(w("ww", h)),
            "sicht": ganz(w("vis", h)),
            "hwind": None if hu is None or hv is None else runde(math.hypot(hu, hv) * 3.6),
            "haus": None if hu is None or hv is None else ganz((math.degrees(math.atan2(-hu, -hv)) + 360.0) % 360.0) % 360,
        })
    return zeilen


def schreibe_json(pfad: Path, kopf: dict, stunden: List[dict]) -> None:
    """Gueltiges JSON, eine Stunde je Zeile (klein und lesbar)."""
    teile = [json.dumps(kopf, ensure_ascii=False)[:-1]]   # ohne schliessende }
    text = teile[0] + ',\n "stunden": [\n' + ",\n".join(
        "  " + json.dumps(s, separators=(", ", ": "), ensure_ascii=False) for s in stunden
    ) + "\n ]\n}\n"
    tmp = pfad.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    json.loads(text)                      # Gegenprobe: ist es gueltig?
    tmp.replace(pfad)


def lese_reviere(pfad: Path, nur: Optional[List[str]]) -> List[dict]:
    daten = json.loads(pfad.read_text(encoding="utf-8"))
    reviere = daten["reviere"]
    namen = [r["name"] for r in reviere]
    if len(set(namen)) != len(namen):
        raise SystemExit("konfig/reviere.json: ein Name kommt doppelt vor")
    for r in reviere:
        if r.get("modell", "auto") not in ("auto", "icon-d2", "icon-eu"):
            raise SystemExit(f"konfig/reviere.json: unbekanntes Modell bei {r['name']}")
    if nur:
        fehlt = [n for n in nur if n not in namen]
        if fehlt:
            raise SystemExit(f"unbekannte Reviere: {', '.join(fehlt)}")
        reviere = [r for r in reviere if r["name"] in nur]
    return reviere


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--konfig", default=str(WURZEL / "konfig" / "reviere.json"))
    ap.add_argument("--ausgabe", default=str(WURZEL / "ausgabe"))
    ap.add_argument("--reviere", help="nur diese (Komma-getrennt), sonst alle")
    ap.add_argument("--zwischenspeicher", default=os.environ.get("WETTER_ZWISCHENSPEICHER"),
                    help="Ordner, in dem geladene Dateien fuer Probelaeufe liegen bleiben")
    ap.add_argument("--arbeiter", type=int, default=4, help="parallele Prozesse (Standard 4)")
    ap.add_argument("-v", "--ausfuehrlich", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.ausfuehrlich else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    start = time.time()

    reviere = lese_reviere(Path(a.konfig), a.reviere.split(",") if a.reviere else None)
    jetzt = dt.datetime.now(dt.timezone.utc)
    aus_ordner = Path(a.ausgabe)
    aus_ordner.mkdir(parents=True, exist_ok=True)

    punkte = {r["name"]: (r["name"], float(r["lat"]), float(r["lon"])) for r in reviere}
    zuordnung: Dict[str, str] = {}      # revier -> "icon-d2" / "icon-eu"
    roh: Dict[str, dict] = {}
    laeufe: Dict[str, dt.datetime] = {}

    # --- 1. ICON-D2 fuer alle, die es nicht ausschliessen -------------------
    d2_kandidaten = [r["name"] for r in reviere if r.get("modell", "auto") != "icon-eu"]
    if d2_kandidaten:
        lauf = neuester_lauf(ICON_D2, jetzt)
        if lauf is not None:
            # Gebietspruefung mit einer kleinen Vorab-Datei (u_10m, Stunde 1).
            u1 = url_zu(ICON_D2, lauf, FELDER[0], 1)
            ordner, h, pr = aufgabe((u1, "u_10m", 1, [punkte[n] for n in d2_kandidaten],
                                     a.zwischenspeicher))
            if pr is None:
                log.error("[ICON-D2] Gebietspruefung nicht moeglich")
            else:
                for n in d2_kandidaten:
                    im_gebiet = pr[n][1]
                    forciert = next(r for r in reviere if r["name"] == n).get("modell") == "icon-d2"
                    if im_gebiet or forciert:
                        zuordnung[n] = "icon-d2"
                    else:
                        log.info("[ICON-D2] %s liegt nicht sicher im Gebiet -> ICON-EU", n)
                if zuordnung:
                    roh["icon-d2"] = lade_modell(ICON_D2, lauf, [punkte[n] for n in zuordnung],
                                                 a.zwischenspeicher, a.arbeiter)
                    laeufe["icon-d2"] = lauf
        else:
            log.warning("ICON-D2 nicht verfuegbar -> betroffene Reviere versuchen ICON-EU")

    # --- 2. ICON-EU fuer den Rest (und als Ausweichen, wenn D2 fehlt) -------
    rest = [r["name"] for r in reviere if r["name"] not in zuordnung]
    if rest:
        lauf = neuester_lauf(ICON_EU, jetzt)
        if lauf is not None:
            for n in rest:
                zuordnung[n] = "icon-eu"
            roh["icon-eu"] = lade_modell(ICON_EU, lauf, [punkte[n] for n in rest],
                                         a.zwischenspeicher, a.arbeiter)
            laeufe["icon-eu"] = lauf

    # --- 3. Dateien schreiben ----------------------------------------------
    erstellt = iso(dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
    index: Dict[str, dict] = {}
    fehlend: List[str] = []
    for r in reviere:
        n = r["name"]
        m = zuordnung.get(n)
        if m is None or m not in roh:
            fehlend.append(n)
            continue
        modell = MODELLE[m]
        stunden = stunden_fuer(n, laeufe[m], modell, roh[m])
        if len(stunden) < 24:
            log.error("[%s] nur %d Stunden mit Wind -> keine Datei", n, len(stunden))
            fehlend.append(n)
            continue
        kopf = {
            "revier": n, "modell": modell.name, "lauf": iso(laeufe[m]), "erstellt": erstellt,
            "punkt": [float(r["lat"]), float(r["lon"])], "quelle": QUELLE,
        }
        schreibe_json(aus_ordner / f"{n}.json", kopf, stunden)
        index[n] = {"lauf": iso(laeufe[m]), "modell": modell.name}
        log.info("[%s] %s, Lauf %s, %d Stunden", n, modell.name, iso(laeufe[m]), len(stunden))

    if index:
        # Teillauf (--reviere): Eintraege der uebrigen, noch vorhandenen Dateien behalten.
        alle = [r["name"] for r in lese_reviere(Path(a.konfig), None)]
        try:
            alt = json.loads((aus_ordner / "index.json").read_text(encoding="utf-8"))["reviere"]
        except (OSError, ValueError, KeyError):
            alt = {}
        for n, e in alt.items():
            if n in alle and n not in index and n not in fehlend and (aus_ordner / f"{n}.json").exists():
                index[n] = e
        index = {n: index[n] for n in alle if n in index}
        (aus_ordner / "index.json").write_text(
            json.dumps({"erstellt": erstellt, "reviere": index}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
    log.info("fertig in %.0f s: %d Reviere, %d ohne Datei%s", time.time() - start, len(index),
             len(fehlend), f" ({', '.join(fehlend)})" if fehlend else "")
    return 0 if index else 1


if __name__ == "__main__":
    sys.exit(main())
