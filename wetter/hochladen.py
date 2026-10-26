#!/usr/bin/env python3
"""Laedt die Wetterdateien des Skipper Simulators (ausgabe/*.json) per FTPS (FTP mit TLS) auf den Webspace.

Uebernommen aus BinnenSkipper-Wetter (pipeline/weather/upload_ftps.py, 25.10.2026), nur Standardordner angepasst.

Nachfolger von ``upload_sftp.py`` seit dem Umzug von huggie.de von Strato zu
ALL-INKL: Dort gibt es fuer ordnerbeschraenkte Zugaenge nur FTPS, kein SFTP.
Kommt mit der Python-Standardbibliothek aus (kein paramiko mehr noetig).

Schnittstelle bewusst wie bei upload_sftp.py, damit die Ablaeufe nur den
Skriptnamen tauschen muessen:

  FTP_HOST         FTP-Server (z. B. w0123456.kasserver.com)        [Pflicht]
  FTP_USER         FTP-Benutzer                                      [Pflicht]
  FTP_PASS         FTP-Passwort                                      [Pflicht]
  FTP_PORT         Port                                              [Standard 21]
  WEATHER_OUT_DIR  lokaler Ordner mit den *.json                     [Standard ausgabe]
  FTP_REMOTE_DIR   Zielordner, RELATIV zum Startordner des Zugangs   [Standard wetter]

SICHERHEIT: Die Verbindung wird per TLS verschluesselt und das Zertifikat des
Servers samt Rechnername geprueft, BEVOR das Passwort gesendet wird. Das ersetzt
die Fingerabdruck-Pruefung (FTP_HOST_KEY) der SFTP-Fassung; FTP_HOST_KEY wird
nicht mehr gebraucht.

Rueckgabewerte wie bisher: 0 ok, 1 Einstellung/keine Dateien, 2 Anmeldung
abgelehnt, 3 sonstiger Fehler.

WIEDERHOLEN wie bisher: Ist der Server nicht erreichbar oder lehnt er die
Anmeldung ab, wird nach 1, 3 und 10 Minuten erneut versucht. NICHT wiederholt
wird ein Zertifikatsfehler: Das ist ein Sicherheitsabbruch, kein Aussetzer.
SFTP_WARTEN (Sekunden, durch Kommas getrennt) aendert die Wartezeiten.
"""

from __future__ import annotations

import ftplib
import glob
import os
import posixpath
import ssl
import sys
import time

WARTEN_STANDARD = (60, 180, 600)


def _clean(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("\r", "").replace("\n", "").strip()


def _wartezeiten() -> tuple[int, ...]:
    roh = _clean(os.environ.get("SFTP_WARTEN"))
    if not roh:
        return WARTEN_STANDARD
    try:
        return tuple(max(0, int(x)) for x in roh.split(",") if x.strip())
    except ValueError:
        return WARTEN_STANDARD


class _Aussetzer(Exception):
    def __init__(self, code: int, meldung: str) -> None:
        super().__init__(meldung)
        self.code = code
        self.meldung = meldung


class _FTPS(ftplib.FTP_TLS):
    """FTP_TLS, das fuer die Datenverbindung die TLS-Sitzung der Steuerverbindung
    wiederverwendet. Viele Server (auch bei ALL-INKL) verlangen das; ohne bricht
    jede Uebertragung mit einem TLS-Fehler ab."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size


def main() -> int:
    host = _clean(os.environ.get("FTP_HOST"))
    user = _clean(os.environ.get("FTP_USER"))
    raw_pass = os.environ.get("FTP_PASS") or ""
    password = raw_pass.replace("\r", "").replace("\n", "").strip()
    port_roh = _clean(os.environ.get("FTP_PORT")) or "21"
    # Steht im Secret noch die 22 der SFTP-Zeit, ist bei FTPS die 21 gemeint.
    port = 21 if port_roh == "22" else int(port_roh)
    out_dir = os.environ.get("WEATHER_OUT_DIR") or "ausgabe"
    remote_dir = (_clean(os.environ.get("FTP_REMOTE_DIR")) or "wetter").strip("/")

    if not host or not user or not password:
        print("::error::FTP_HOST / FTP_USER / FTP_PASS secret is missing.", file=sys.stderr)
        return 1

    files = sorted(glob.glob(os.path.join(out_dir, "*.json")))
    if not files:
        print(f"::error::no JSON files found in {out_dir}", file=sys.stderr)
        return 1

    print(f"user length={len(user)} pass length={len(password)}")
    print(f"Connecting to {host}:{port} (FTPS) as <user> -> uploading {len(files)} file(s) to {remote_dir}/")

    warten = _wartezeiten()
    versuche = len(warten) + 1
    for nr in range(1, versuche + 1):
        try:
            return _hochladen(host, port, user, password, files, remote_dir)
        except _Aussetzer as fehler:
            if nr < versuche:
                pause = warten[nr - 1]
                print(f"::warning::Versuch {nr} von {versuche}: {fehler.meldung} -- neuer Versuch in {pause} s.")
                time.sleep(pause)
                continue
            gesamt = sum(warten) // 60
            if fehler.code == 2:
                print(
                    "::error::FTPS authentication failed: Der Server hat die Anmeldung auch nach "
                    f"{versuche} Versuchen ueber {gesamt} Minuten abgelehnt. Klappt der naechste Lauf "
                    "wieder, lag es am Server des Anbieters. Haelt es an: FTP_USER / FTP_PASS pruefen.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"::error::FTPS upload failed nach {versuche} Versuchen ueber {gesamt} Minuten: "
                    f"{fehler.meldung}",
                    file=sys.stderr,
                )
            return fehler.code
    return 3


def _hochladen(host: str, port: int, user: str, password: str, files: list[str], remote_dir: str) -> int:
    """Ein Versuch. Wirft _Aussetzer, wenn es sich lohnt, es gleich noch einmal zu versuchen."""
    kontext = ssl.create_default_context()  # prueft Zertifikat UND Rechnername
    ftp = _FTPS(context=kontext, timeout=30)
    try:
        try:
            ftp.connect(host, port)
        except Exception as exc:  # noqa: BLE001 - Verbindung kam gar nicht zustande
            raise _Aussetzer(3, f"Server nicht erreichbar ({exc})") from exc
        try:
            ftp.auth()  # TLS aushandeln, Zertifikat pruefen -- VOR dem Passwort
        except ssl.SSLCertVerificationError as exc:
            print(
                f"::error::TLS-Zertifikat des Servers ist nicht gueltig ({exc}) -- Upload "
                "abgebrochen (moeglicher Man-in-the-Middle).",
                file=sys.stderr,
            )
            return 3
        except Exception as exc:  # noqa: BLE001
            raise _Aussetzer(3, f"TLS-Aufbau gescheitert ({exc})") from exc
        print("TLS-Zertifikat geprueft (%s)." % ftp.sock.version())
        try:
            ftp.login(user, password, secure=False)  # TLS steht schon
        except ftplib.error_perm as exc:
            raise _Aussetzer(2, "Server hat die Anmeldung abgelehnt") from exc
        ftp.prot_p()  # auch die Daten verschluesseln
        ftp.set_pasv(True)

        # Zielordner anlegen (vorhandene Teile ueberspringen).
        cur = ""
        for part in remote_dir.split("/"):
            cur = part if not cur else posixpath.join(cur, part)
            try:
                ftp.mkd(cur)
                print(f"  mkdir {cur}")
            except ftplib.error_perm:
                pass  # gibt es schon

        for local in files:
            remote = posixpath.join(remote_dir, os.path.basename(local))
            with open(local, "rb") as handle:
                ftp.storbinary(f"STOR {remote}", handle)
            print(f"  put {os.path.basename(local)} -> {remote}")

        # Skipper Simulator: .htaccess mit Access-Control-Allow-Origin — das Spiel laeuft in der App als lokale Seite und
        # darf die Wetterdateien sonst nicht lesen (CORS). Liegt neben diesem Skript; wird jedes Mal mitgeschickt.
        zugang = os.path.join(os.path.dirname(os.path.abspath(__file__)), "htaccess-wetter")
        if os.path.exists(zugang):
            with open(zugang, "rb") as handle:
                ftp.storbinary(f"STOR {posixpath.join(remote_dir, '.htaccess')}", handle)
            print(f"  put htaccess-wetter -> {posixpath.join(remote_dir, '.htaccess')}")

        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 - Abmelden darf scheitern, die Dateien sind oben
            pass
        print("FTPS upload OK")
        return 0
    except _Aussetzer:
        raise
    except Exception as exc:  # noqa: BLE001 - Abbruch mitten im Hochladen: ebenfalls neu versuchen
        raise _Aussetzer(3, f"Hochladen abgebrochen ({exc})") from exc
    finally:
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
