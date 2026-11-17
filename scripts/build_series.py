import json
import os
import re
import pandas as pd
import requests
from urllib.parse import urljoin

ANNI = [str(y) for y in range(2002, 2027)]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PopolazioneRicostruita/1.0"}

def carica_dati_situas():
    with open("data/raw/comuni_attuali.json", encoding="utf-8") as f:
        comuni = pd.DataFrame(json.load(f))
    with open("data/raw/variazioni_storiche.json", encoding="utf-8") as f:
        variazioni = pd.DataFrame(json.load(f))
    with open("data/raw/raccordo_2019_2026.json", encoding="utf-8") as f:
        raccordo = pd.DataFrame(json.load(f))
    return comuni, variazioni, raccordo

def trova_link_download_demo(codice_app):
    """Estrae dinamicamente i veri URL dei file CSV/ZIP dalla pagina Area Download di Demo Istat."""
    url_pagina = f"https://demo.istat.it/app/?i={codice_app}&l=it"
    r = requests.get(url_pagina, headers=HEADERS, timeout=60)
    r.raise_for_status()
    # Cerca tutti i link a file .zip o .csv nella pagina o nei bundle JS di configurazione
    links = re.findall(r'["\']([^"\']+\.(?:zip|csv))["\']', r.text, flags=re.IGNORECASE)
    return [urljoin("https://demo.istat.it/", l.lstrip("/")) for l in set(links)]

def carica_dati_demo_istat():
    pop_storica = {}  # {cod_6_cifre: {anno: int}}
    spostamenti = {}  # {(cod_6_cifre, anno): int}

    def salva_pop(cod, anno, val):
        if pd.notna(val) and val > 0:
            pop_storica.setdefault(cod, {})[str(anno)] = int(round(val))

    # Diagnostica reale sugli endpoint di Demo Istat
    for app_code in ["RIC", "P02", "POS", "D7B"]:
        trovati = trova_link_download_demo(app_code)
        print(f"Link trovati su Demo Istat ({app_code}): {trovati[:5]} (totale {len(trovati)})")

    # Interrogazione diretta API JSON/CSV di Demo Istat (endpoint utilizzato dal frontend demo.istat.it)
    # 1. Ricostruzione 2002-2019
    for anno in range(2002, 2019):
        url_api_ric = f"https://istat.it_{anno}_it_Comuni.zip"
        r = requests.head(url_api_ric, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            df = pd.read_csv(url_api_ric, compression="zip", sep=";", encoding="latin1", low_memory=False)
            col_cod = [c for c in df.columns if "Codice" in c or "PRO_COM" in c]
            col_tot = [c for c in df.columns if "Totale" in c or "Popolazione" in c][-1]
            if "Età" in df.columns:
                df = df[df["Età"].astype(str).str.contains("Totale|999", na=True)]
            for _, row in df.iterrows():
                salva_pop(str(row[col_cod]).strip().zfill(6), anno, pd.to_numeric(row[col_tot], errors="coerce"))
            print(f"Caricato {anno} da {url_api_ric}")

    # Se i file annuali RIC non esistono con quel nome, prova il file unico multianno
    if not any("2002" in v for v in pop_storica.values()):
        for candidato in [
            "https://istat.it_2002-2019_it_Comuni.zip",
            "https://istat.it_2002_2019_Comuni.zip",
            "https://istat.itostruzione_Comuni_2002_2019.zip"
        ]:
            if requests.head(candidato, headers=HEADERS, timeout=30).status_code == 200:
                df = pd.read_csv(candidato, compression="zip", sep=";", encoding="latin1", low_memory=False)
                print(f"Trovato file unico: {candidato} -> colonne: {df.columns.tolist()}")
                break

    # Controllo bloccante anti-zero (verifica Roma = 058091)
    if pop_storica.get("058091", {}).get("2002", 0) == 0:
        raise RuntimeError(
            "BLOCCO DI SICUREZZA: I dati scaricati da Demo Istat sono ancora vuoti (Roma 2002 = 0). "
            "Controlla i log sopra 'Link trovati su Demo Istat' per vedere il percorso esatto dei file."
        )

    return pop_storica, spostamenti

def ricostruisci():
    comuni_2026, variazioni, raccordo = carica_dati_situas()
    pop_storica, spostamenti_demo = carica_dati_demo_istat()
    # ... (il resto del salvataggio rimane identico)

if __name__ == "__main__":
    ricostruisci()
