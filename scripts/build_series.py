import json
import os
import pandas as pd
import numpy as np
import requests
from io import StringIO

ANNI = [str(y) for y in range(2002, 2027)]

def carica_dati_situas():
    with open("data/raw/comuni_attuali.json", encoding="utf-8") as f:
        comuni = pd.DataFrame(json.load(f))
    with open("data/raw/variazioni_storiche.json", encoding="utf-8") as f:
        variazioni = pd.DataFrame(json.load(f))
    with open("data/raw/raccordo_2019_2026.json", encoding="utf-8") as f:
        raccordo = pd.DataFrame(json.load(f))
    
    print("Colonne comuni_attuali:", comuni.columns.tolist())
    print("Colonne variazioni_storiche:", variazioni.columns.tolist())
    print("Colonne raccordo_2019_2026:", raccordo.columns.tolist())
    return comuni, variazioni, raccordo

def scarica_sdmx_istat(url, nome_dataset):
    print(f"Scaricamento {nome_dataset} da Istat SDMX...")
    headers = {"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}
    resp = requests.get(url, headers=headers, timeout=180)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), low_memory=False)
    print(f"Scaricate {len(df)} righe per {nome_dataset}. Colonne: {df.columns.tolist()}")
    return df

def ricostruisci():
    comuni_2026, variazioni, raccordo = carica_dati_situas()

    # 1. Scarica popolazione ricostruita 2002-2019 (ai confini 2019) e bilancio 2019-2026
    # Dataflow Istat: DCIS_POPRES1 (Popolazione residente) e DCIS_RICOSTPOP2001_2019 (Ricostruzione 2002-2019)
    url_ric = "https://istat.it"
    url_bil = "https://istat.it"

    df_ric = scarica_sdmx_istat(url_ric, "Ricostruzione 2002-2019")
    df_bil = scarica_sdmx_istat(url_bil, "Popolazione 2019-2026")

    # Normalizzazione codice Istat a 6 cifre con zeri iniziali (PRO_COM_T)
    comuni_2026["PRO_COM_T"] = comuni_2026["PRO_COM_T"].astype(str).str.zfill(6)
    
    # Individuazione colonne codice e valore nei CSV SDMX Istat
    col_geo_ric = [c for c in df_ric.columns if "REF_AREA" in c or "ITTER107" in c][0]
    col_geo_bil = [c for c in df_bil.columns if "REF_AREA" in c or "ITTER107" in c][0]

    df_ric["COD"] = df_ric[col_geo_ric].astype(str).str.zfill(6)
    df_bil["COD"] = df_bil[col_geo_bil].astype(str).str.zfill(6)

    # Pivot per avere una riga per codice comunale e una colonna per anno
    piv_ric = df_ric.pivot_table(index="COD", columns="TIME_PERIOD", values="OBS_VALUE", aggfunc="first")
    piv_bil = df_bil.pivot_table(index="COD", columns="TIME_PERIOD", values="OBS_VALUE", aggfunc="first")
    piv_ric.columns = piv_ric.columns.astype(str)
    piv_bil.columns = piv_bil.columns.astype(str)

    # Costruzione mappa di raccordo dal 2019 ai codici vigenti 2026
    # Trova le colonne di codice origine e codice destinazione nel report 99 di SITUAS
    col_old = [c for c in raccordo.columns if "PRO_COM_T" in c and ("INI" in c or "OLD" in c or "1" in c or c == "PRO_COM_T")][0]
    col_new = [c for c in raccordo.columns if "PRO_COM_T" in c and c != col_old][-1] if len([c for c in raccordo.columns if "PRO_COM_T" in c]) > 1 else col_old

    raccordo[col_old] = raccordo[col_old].astype(str).str.zfill(6)
    raccordo[col_new] = raccordo[col_new].astype(str).str.zfill(6)

    mappa_new_to_olds = raccordo.groupby(col_new)[col_old].apply(list).to_dict()

    risultati_json = []
    risultati_csv = []

    for _, row in comuni_2026.iterrows():
        cod_2026 = row["PRO_COM_T"]
        denom = row.get("COMUNE", row.get("DENOMINAZIONE", ""))
        sigla = row.get("SIGLA_AUTOMOBILISTICA", row.get("SIGLA", ""))

        codici_origine = mappa_new_to_olds.get(cod_2026, [cod_2026])
        is_fusione = len(codici_origine) > 1

        serie = {}
        riga_csv = {"codice_istat": cod_2026, "denominazione": denom, "sigla_prov": sigla}
        anni_somma = []
        anni_prop = []

        for anno in ANNI:
            val = 0
            tipo = "R"

            if int(anno) < 2019:
                # Dati dalla ricostruzione 2002-2019
                sotto = piv_ric.reindex(codici_origine)[anno].dropna() if anno in piv_ric.columns else pd.Series(dtype=float)
                val = int(round(sotto.sum())) if not sotto.empty else 0
                if is_fusione and val > 0:
                    tipo = "S"
                    anni_somma.append(anno)
            else:
                # Dati dal bilancio 2019-2026
                if cod_2026 in piv_bil.index and anno in piv_bil.columns and pd.notna(piv_bil.loc[cod_2026, anno]):
                    val = int(round(piv_bil.loc[cod_2026, anno]))
                    tipo = "R"
                else:
                    # Comune non ancora fuso in quell'anno post-2019: somma i predecessori
                    sotto = piv_bil.reindex(codici_origine)[anno].dropna() if anno in piv_bil.columns else pd.Series(dtype=float)
                    val = int(round(sotto.sum())) if not sotto.empty else 0
                    if is_fusione and val > 0:
                        tipo = "S"
                        anni_somma.append(anno)

            serie[anno] = {"v": val, "t": tipo}
            riga_csv[anno] = val

        note = []
        if anni_somma:
            note.append(f"Fusione (somma comuni originari: {', '.join(codici_origine)})")
        if anni_prop:
            note.append("Stima proporzionale arrotondata all'unità")

        riga_csv["anni_ricostruiti_somma"] = ",".join(anni_somma)
        riga_csv["anni_stimati_proporzione"] = ",".join(anni_prop)
        riga_csv["note_ricostruzione"] = " | ".join(note)

        risultati_json.append({
            "codice_istat": cod_2026,
            "denominazione": denom,
            "sigla_prov": sigla,
            "note": " | ".join(note),
            "serie": serie
        })
        risultati_csv.append(riga_csv)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(risultati_csv).to_csv("data/popolazione_2026.csv", index=False, encoding="utf-8")
    with open("data/popolazione_2026.json", "w", encoding="utf-8") as f:
        json.dump(risultati_json, f, ensure_ascii=False, separators=(",", ":"))
    
    print(f"Completato! Salvati {len(risultati_json)} comuni in data/popolazione_2026.json e .csv")

if __name__ == "__main__":
    ricostruisci()
