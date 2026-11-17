import json
import os
import zipfile
import pandas as pd
import numpy as np
import requests
from io import StringIO, BytesIO

ANNI = [str(y) for y in range(2002, 2027)]

def carica_dati_situas():
    with open("data/raw/comuni_attuali.json", encoding="utf-8") as f:
        comuni = pd.DataFrame(json.load(f))
    with open("data/raw/variazioni_storiche.json", encoding="utf-8") as f:
        variazioni = pd.DataFrame(json.load(f))
    with open("data/raw/raccordo_2019_2026.json", encoding="utf-8") as f:
        raccordo = pd.DataFrame(json.load(f))
    return comuni, variazioni, raccordo

def scarica_sdmx_istat(url, nome_dataset):
    print(f"Scaricamento {nome_dataset} da sdmx.istat.it...")
    headers = {
        "Accept": "application/vnd.sdmx.data+csv;version=1.0.0",
        "User-Agent": "Mozilla/5.0 (compatible; PopolazioneRicostruitaBot/1.0)"
    }
    resp = requests.get(url, headers=headers, timeout=180)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), low_memory=False)
    print(f"Scaricate {len(df)} righe per {nome_dataset}. Colonne: {df.columns.tolist()}")
    return df

def scarica_variazioni_demografiche_demo():
    """
    Scarica dai bilanci demografici annuali di demo.istat.it (2019-2025)
    il campo 'Variazioni territoriali' (abitanti spostati) per ciascun comune.
    """
    spostamenti = {} # chiave: (codice_comune, anno_str) -> int abitanti spostati
    for anno in range(2019, 2026):
        url_zip = f"https://istat.it_{anno}_it_Comuni.zip"
        try:
            r = requests.get(url_zip, timeout=60)
            if r.status_code == 200:
                with zipfile.ZipFile(BytesIO(r.content)) as z:
                    csv_name = [n for n in z.namelist() if n.endswith(".csv")]
                    with z.open(csv_name) as f:
                        df_b = pd.read_csv(f, sep=";", encoding="latin1", low_memory=False)
                        col_cod = [c for c in df_b.columns if "Codice" in c or "PRO_COM" in c]
                        col_var = [c for c in df_b.columns if "territorial" in c.lower()]
                        if col_var:
                            for _, row in df_b.iterrows():
                                cod = str(row[col_cod]).strip().zfill(6)
                                val = pd.to_numeric(row[col_var], errors="coerce")
                                if pd.notna(val) and val != 0:
                                    spostamenti[(cod, str(anno))] = int(round(val))
                print(f"Bilancio Demo Istat {anno}: caricati {sum(1 for k in spostamenti if k==str(anno))} eventi territoriali")
        except Exception as e:
            print(f"Avviso scaricamento Demo Istat {anno}: {e}")
    return spostamenti

def ricostruisci():
    comuni_2026, variazioni, raccordo = carica_dati_situas()

    # Endpoint ufficiali attivi sdmx.istat.it
    url_ric = "https://istat.it"
    url_bil = "https://istat.it"

    df_ric = scarica_sdmx_istat(url_ric, "Ricostruzione 2002-2019")
    df_bil = scarica_sdmx_istat(url_bil, "Popolazione 2019-2026")
    spostamenti_demo = scarica_variazioni_demografiche_demo()

    # Normalizzazione codici SITUAS
    comuni_2026["PRO_COM_T"] = comuni_2026["PRO_COM_T"].astype(str).str.strip().str.zfill(6)
    raccordo["PRO_COM_T_DT_IN"] = raccordo["PRO_COM_T_DT_IN"].astype(str).str.strip().str.zfill(6)
    raccordo["PRO_COM_T_DT_FI"] = raccordo["PRO_COM_T_DT_FI"].astype(str).str.strip().str.zfill(6)

    # Pivot dati demografici SDMX
    col_geo_ric = [c for c in df_ric.columns if "REF_AREA" in c or "ITTER107" in c]
    col_geo_bil = [c for c in df_bil.columns if "REF_AREA" in c or "ITTER107" in c]

    df_ric["COD"] = df_ric[col_geo_ric].astype(str).str.strip().str.zfill(6)
    df_bil["COD"] = df_bil[col_geo_bil].astype(str).str.strip().str.zfill(6)

    piv_ric = df_ric.pivot_table(index="COD", columns="TIME_PERIOD", values="OBS_VALUE", aggfunc="first")
    piv_bil = df_bil.pivot_table(index="COD", columns="TIME_PERIOD", values="OBS_VALUE", aggfunc="first")
    piv_ric.columns = piv_ric.columns.astype(str)
    piv_bil.columns = piv_bil.columns.astype(str)

    # Mappa fusioni/raccordi 2019 -> 2026
    mappa_new_to_olds = raccordo.groupby("PRO_COM_T_DT_FI")["PRO_COM_T_DT_IN"].apply(lambda x: sorted(list(set(x)))).to_dict()

    # Costruzione serie base (Reali + Fusioni per somma)
    serie_comuni = {}
    tipi_comuni = {}
    note_comuni = {}

    for _, row in comuni_2026.iterrows():
        cod_2026 = row["PRO_COM_T"]
        codici_origine = mappa_new_to_olds.get(cod_2026, [cod_2026])
        is_fusione = len(codici_origine) > 1

        serie_comuni[cod_2026] = {}
        tipi_comuni[cod_2026] = {}
        note_comuni[cod_2026] = []

        if is_fusione:
            note_comuni[cod_2026].append(f"Fusione (somma comuni originari: {', '.join(codici_origine)})")

        for anno in ANNI:
            if int(anno) < 2019:
                sotto = piv_ric.reindex(codici_origine)[anno].dropna() if anno in piv_ric.columns else pd.Series(dtype=float)
                val = int(round(sotto.sum())) if not sotto.empty else 0
                serie_comuni[cod_2026][anno] = val
                tipi_comuni[cod_2026][anno] = "S" if (is_fusione and val > 0) else "R"
            else:
                if cod_2026 in piv_bil.index and anno in piv_bil.columns and pd.notna(piv_bil.loc[cod_2026, anno]):
                    val = int(round(piv_bil.loc[cod_2026, anno]))
                    serie_comuni[cod_2026][anno] = val
                    tipi_comuni[cod_2026][anno] = "R"
                else:
                    sotto = piv_bil.reindex(codici_origine)[anno].dropna() if anno in piv_bil.columns else pd.Series(dtype=float)
                    val = int(round(sotto.sum())) if not sotto.empty else 0
                    serie_comuni[cod_2026][anno] = val
                    tipi_comuni[cod_2026][anno] = "S" if (is_fusione and val > 0) else "R"

    # Gestione riproporzionamento per variazioni territoriali parziali (scorpori/cessioni 2019-2026)
    var_recenti = variazioni[pd.to_numeric(variazioni["ANNO"], errors="coerce") >= 2019].copy()
    for _, v in var_recenti.iterrows():
        desc = str(v.get("DESC_COD_VARIAZIONE", "")).lower()
        # Escludi fusioni totali (già sommate) e cambi di denominazione
        if "cessione" in desc or "distacco" in desc or "scorporo" in desc or "aggregazione parziale" in desc:
            anno_ev = str(int(v["ANNO"]))
            cod_c1 = str(v["PRO_COM_T"]).strip().zfill(6)
            cod_c2 = str(v["PRO_COM_T_REL"]).strip().zfill(6)
            
            # Recupera abitanti spostati dal bilancio Demo Istat nell'anno dell'evento
            delta_1 = spostamenti_demo.get((cod_c1, anno_ev), 0)
            if delta_1 < 0 and cod_c1 in serie_comuni and cod_c2 in serie_comuni:
                cedente, ricevente, delta_p = cod_c1, cod_c2, abs(delta_1)
                anno_prec = str(int(anno_ev) - 1)
                pop_ced_prec = serie_comuni[cedente].get(anno_prec, 0)
                if pop_ced_prec > 0:
                    alpha = delta_p / pop_ced_prec
                    note_comuni[cedente].append(f"Cessione territoriale {anno_ev} (-{delta_p} ab., coeff. {alpha:.4f})")
                    note_comuni[ricevente].append(f"Acquisizione territoriale {anno_ev} (+{delta_p} ab., coeff. {alpha:.4f})")
                    for anno in ANNI:
                        if int(anno) < int(anno_ev):
                            quota = int(round(serie_comuni[cedente][anno] * alpha))
                            serie_comuni[cedente][anno] -= quota
                            serie_comuni[ricevente][anno] += quota
                            tipi_comuni[cedente][anno] = "P"
                            tipi_comuni[ricevente][anno] = "P"

    # Esportazione JSON (per Web App) e CSV (con colonne anni stimati)
    risultati_json = []
    risultati_csv = []

    for _, row in comuni_2026.iterrows():
        cod_2026 = row["PRO_COM_T"]
        denom = row["COMUNE"]
        sigla = row["SIGLA_AUTOMOBILISTICA"]

        serie_obj = {a: {"v": serie_comuni[cod_2026][a], "t": tipi_comuni[cod_2026][a]} for a in ANNI}
        anni_s = [a for a in ANNI if tipi_comuni[cod_2026][a] == "S"]
        anni_p = [a for a in ANNI if tipi_comuni[cod_2026][a] == "P"]
        nota_str = " | ".join(dict.fromkeys(note_comuni[cod_2026]))

        riga_csv = {"codice_istat": cod_2026, "denominazione": denom, "sigla_prov": sigla}
        for a in ANNI:
            riga_csv[a] = serie_comuni[cod_2026][a]
        riga_csv["anni_ricostruiti_somma"] = ",".join(anni_s)
        riga_csv["anni_stimati_proporzione"] = ",".join(anni_p)
        riga_csv["note_ricostruzione"] = nota_str

        risultati_json.append({
            "codice_istat": cod_2026,
            "denominazione": denom,
            "sigla_prov": sigla,
            "note": nota_str,
            "serie": serie_obj
        })
        risultati_csv.append(riga_csv)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(risultati_csv).to_csv("data/popolazione_2026.csv", index=False, encoding="utf-8")
    with open("data/popolazione_2026.json", "w", encoding="utf-8") as f:
        json.dump(risultati_json, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Completato! Generati {len(risultati_json)} comuni in data/popolazione_2026.json e .csv")

if __name__ == "__main__":
    ricostruisci()

