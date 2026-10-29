"""Sturmwarnung Bodensee fuer den Skipper Simulator (29.10.2026).

Holt den WarnWetter-Feed des Deutschen Wetterdienstes (gemeinde_warnings_v2.json, Feld ``binnenSee``) und schreibt
``ausgabe-sturm/sturmwarn-bodensee.json``::

    { "zeit": "2026-10-29T12:00:00Z", "quelle": "Deutscher Wetterdienst (WarnWetter)",
      "bereiche": { "west": 0, "mitte": 1, "ost": 2 } }

Stufen wie die echten Leuchten: 0 aus, 1 Starkwindwarnung (40 Blitze/min), 2 Sturmwarnung (90 Blitze/min).
Warnzellen des DWD (cap_warncellids): Bodensee-West 208439000, Bodensee-Mitte 208438000, Bodensee-Ost 208440000.
Zuordnung wie BinnenSkipper (stormlight.php, classify): Stufe >= 3 oder „Sturm/Orkan“ im Text = Sturm; sonst Eintrag vorhanden = Starkwind.
Spielwetter, kein Warndienst. Nur Standardbibliothek. Aufruf: python wetter/sturmwarn_bodensee.py
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time
import urllib.request

FEED = "https://s3.eu-central-1.amazonaws.com/app-prod-static.warnwetter.de/v16/gemeinde_warnings_v2.json"
ZELLEN = {"west": "208439000", "mitte": "208438000", "ost": "208440000"}
AUS = os.environ.get("STURM_OUT_DIR") or "ausgabe-sturm"


def holen() -> dict:
    anfrage = urllib.request.Request(FEED, headers={"User-Agent": "SkipperSimulator-Wetter/1.0"})
    with urllib.request.urlopen(anfrage, timeout=30) as antwort:
        roh = antwort.read()
    if roh[:2] == b"\x1f\x8b":
        roh = gzip.decompress(roh)
    return json.loads(roh.decode("utf-8"))


def schwerste(eintrag) -> dict | None:
    if isinstance(eintrag, list):
        kandidaten = [e for e in eintrag if isinstance(e, dict)]
    elif isinstance(eintrag, dict) and isinstance(eintrag.get("warnings"), list):
        kandidaten = [e for e in eintrag["warnings"] if isinstance(e, dict)]
    elif isinstance(eintrag, dict):
        kandidaten = [eintrag]
    else:
        return None
    return max(kandidaten, key=lambda e: int(e.get("level") or -1), default=None)


def stufe(eintrag) -> int:
    if eintrag is None:
        return 0
    o = schwerste(eintrag)
    if o is None:
        return 1  # Eintrag da, aber unlesbar: lieber warnen
    level = o.get("level")
    text = " ".join(str(o.get(k) or "") for k in ("event", "headline", "description")).lower()
    if (isinstance(level, (int, float)) and level >= 3) or "sturm" in text or "orkan" in text:
        return 2
    return 1


def main() -> int:
    try:
        feed = holen()
    except Exception as fehler:  # noqa: BLE001 — jeder Fehler: alte Datei bleibt, Lauf rot
        print(f"::error::WarnWetter-Feed nicht lesbar: {fehler}", file=sys.stderr)
        return 1
    see = feed.get("binnenSee") if isinstance(feed.get("binnenSee"), dict) else {}
    bereiche = {}
    for name, zelle in ZELLEN.items():
        eintrag = see.get(zelle)
        if eintrag is None:  # Rückfall: Zelle über den Text finden
            muster = re.compile(r"bodensee\s*-?\s*" + {"west": "west", "mitte": "mitte", "ost": "ost"}[name], re.I)
            eintrag = next((v for v in see.values() if muster.search(json.dumps(v, ensure_ascii=False))), None)
        bereiche[name] = stufe(eintrag)
    os.makedirs(AUS, exist_ok=True)
    aus = {"zeit": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "quelle": "Deutscher Wetterdienst (WarnWetter)", "bereiche": bereiche}
    with open(os.path.join(AUS, "sturmwarn-bodensee.json"), "w", encoding="utf-8") as f:
        json.dump(aus, f, ensure_ascii=False)
    print(json.dumps(aus, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
