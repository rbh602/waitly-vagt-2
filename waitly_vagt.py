#!/usr/bin/env python3
"""
Waitly-vagt: holder øje med antallet af opskrivninger på Waitlys offentlige
foreningssider (waitly.eu) og sender push-beskeder via ntfy.sh, når:

- en overvåget liste falder i antal   -> der kan være en plads fri
- en overvåget liste stiger i antal   -> listen tager imod tilmeldinger (eller er fyldt igen)
- en helt ny liste dukker op          -> uanset navn
- en overvåget liste forsvinder       -> fx omdøbt, så dit filter skal rettes

Foreningerne står i foreninger.json. ntfy-topic'et læses fra miljøvariablen NTFY_TOPIC
(på GitHub: en repository secret). Tilstanden gemmes i waitly_state.json.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

MAPPE = Path(__file__).resolve().parent
CONFIG_FILE = MAPPE / "foreninger.json"
STATE_FILE = MAPPE / "waitly_state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
HEADERS = {"User-Agent": "Personlig Waitly-overvaagning (1 opslag pr. 30 min)"}
COUNT_RE = re.compile(r"^(\d+)\s+opskrivninger?$", re.IGNORECASE)


def hent_side(url: str) -> tuple[str, dict[str, int]]:
    """Returnerer (foreningens navn, {listenavn: antal opskrivninger})."""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1")
    titel = h1.get_text(" ", strip=True) if h1 else url.rstrip("/").split("/")[-1]

    lister = {}
    for tekst in soup.find_all(string=re.compile(r"opskrivning", re.IGNORECASE)):
        blok = tekst.parent
        m = COUNT_RE.match(blok.get_text(" ", strip=True))
        if not m and blok.parent is not None:  # tallet kan ligge i et søskende-element
            blok = blok.parent
            m = COUNT_RE.match(blok.get_text(" ", strip=True))
        if not m:
            continue
        overskrift = blok.find_previous(["h2", "h3", "h4"])  # listens navn står i overskriften før
        if overskrift:
            lister[overskrift.get_text(" ", strip=True)] = int(m.group(1))
    return titel, lister


def send(titel: str, besked: str, url: str | None = None, prioritet: int = 3) -> None:
    print(f"{titel}: {besked}")
    if not NTFY_TOPIC:
        return
    data = {"topic": NTFY_TOPIC, "title": titel, "message": besked,
            "priority": prioritet, "tags": ["house"]}
    if url:
        data["click"] = url
    try:
        requests.post("https://ntfy.sh/", json=data, timeout=15).raise_for_status()
    except requests.RequestException as e:
        print(f"Kunne ikke sende besked: {e}")


def matcher(navn: str, ord: list[str]) -> bool:
    return any(o.lower() in navn.lower() for o in ord)


def find_max(forening: dict, navn: str):
    return next((v for k, v in forening.get("max", {}).items() if k.lower() in navn.lower()), None)


def tjek_forening(f: dict, gammel: dict) -> dict:
    url = f["url"]
    titel, alle = hent_side(url)
    if not alle:
        raise ValueError("fandt ingen lister - sidens layout er måske ændret")

    kendte = {k: v for k, v in gammel.items() if not k.startswith("_")}
    filtre = f.get("lister", [])
    overvaaget = lambda navn: not filtre or matcher(navn, filtre)

    if not kendte:  # foreningen er lige tilføjet: send en oversigt som kvittering
        oversigt = "\n".join(f"{'> ' if overvaaget(n) else ''}{n}: {a}" for n, a in alle.items())
        send(f"Overvåger nu {titel}", oversigt, url, 2)
        return alle

    for navn, antal in alle.items():
        foer = kendte.get(navn)
        if foer is None:
            send("Ny liste!", f"{titel}: {navn} ({antal} opskrivninger)", url, 5)
            continue
        if not overvaaget(navn) or antal == foer:
            continue
        maks = find_max(f, navn)
        if antal < foer:
            ekstra = f" - {maks - antal} under loftet" if maks and antal < maks else ""
            send("Plads fri?", f"{titel}: {navn}\n{foer} -> {antal}{ekstra}", url, 5)
        elif maks and antal >= maks:
            send("Listen er fyldt igen", f"{titel}: {navn}\n{foer} -> {antal}", url, 2)
        else:
            send("Listen tager imod tilmeldinger?", f"{titel}: {navn}\n{foer} -> {antal}", url, 5)

    for navn in kendte.keys() - alle.keys():
        if overvaaget(navn):
            send("Liste forsvundet", f"{titel}: '{navn}' vises ikke længere. "
                 "Er den omdøbt, så tjek dit filter i foreninger.json.", url, 3)
    return alle


def main() -> None:
    if not NTFY_TOPIC:
        print("NTFY_TOPIC mangler - tilføj den som repository secret på GitHub.")
        sys.exit(1)
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        foreninger = config["foreninger"]
    except Exception as e:
        send("Waitly-vagten: fejl i foreninger.json", str(e), prioritet=4)
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    ny_state = {}
    for f in foreninger:
        url = f["url"]
        gammel = state.get(url, {})
        try:
            ny_state[url] = tjek_forening(f, gammel)
        except Exception as e:
            if not gammel.get("_fejl"):  # kun én fejlbesked, ikke én hver halve time
                send("Waitly-vagten kan ikke læse en side", f"{url}\n{e}", url, 3)
            ny_state[url] = {**gammel, "_fejl": True}
        time.sleep(2)

    STATE_FILE.write_text(json.dumps(ny_state, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")


if __name__ == "__main__":
    main()
