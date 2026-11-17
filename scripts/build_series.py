import json
import os
import pandas as pd
import requests

ANNI = [str(y) for y in range(2002, 2027)]

def carica_dati_situas():
    with open("data/raw/comuni_attuali.json", encoding="utf-8") as f:
        comuni = pd.DataFrame(json.load(f))
    with open("data/raw/variazioni_storiche.json", encoding="utf-8") as f:
        variazioni = pd.DataFrame(json.load(f))
    with open("data/raw/raccordo_2019_2026.json", encoding="utf-8") as f:
        raccordo = pd.DataFrame(json.load(f))
    return comuni, variazioni, raccordo

def carica_dati_demo_istat():
    """
    Legge direttamente i CSV da demo.istat.it tramite pandas.read_csv
    (nessuna gestione manuale di archivi o API SDMX con redirect).
    """
    pop_storica = {}  # {cod_6_cifre: {anno: int}}
    spostamenti = {}  # {(cod_6_cifre, anno): int}

    def salva_pop(cod, anno, val):
        pop_storica.setdefault(cod, {})[str(anno)] = int(round(val))

    # 1. Ricostruzione 2002-2019 (ai confini del 1° gennaio 2019)
    url_ric = "https://istat.it"
    try:
        df_ric = pd.read_csv(url_ric, compression="zip", sep=";", encoding="latin1", low_memory=False)
        col_cod = [c for c in df_ric.columns if "Codice" in c or "PRO_COM" in c]
        if "Età" in df_ric.columns:
            df_ric = df_ric[df_ric["Età"].astype(str).str.contains("Totale|999", na=True)]
        for _, row in df_ric.iterrows():
            cod = str(row[col_cod]).strip().zfill(6)
            if "Anno" in df_ric.columns:
                col_tot = [c for c in df_ric.columns if "Totale" in c or "Popolazione" in c][-1]
                val = pd.to_numeric(row[col_tot], errors="coerce")
                if pd.notna(val):
                    salva_pop(cod, int(row["Anno"]), val)
            else:
                for a in range(2002, 2020):
                    if str(a) in df_ric.columns:
                        val = pd.to_numeric(row[str(a)], errors="coerce")
                        if pd.notna(val):
                            salva_pop(cod, a, val)
        print(f"Ricostruzione 2002-2019 caricata: {len(pop_storica)} comuni")
    except Exception as e:
        print(f"Errore lettura {url_ric}: {e}")

    # 2. Bilanci demografici 2019-2026 (con colonna variazioni territoriali)
    for anno in range(2019, 2027):
        for prefisso in ["p02/P02", "pos/POS", "d7b/D7B"]:
            url = f"https://istat.it{prefisso}_{anno}_it_Comuni.zip"
            try:
                df = pd.read_csv(url, compression="zip", sep=";", encoding="latin1", low_memory=False)
                col_cod = [c for c in df.columns if "Codice" in c or "PRO_COM" in c]
                col_pop = [c for c in df.columns if "Popolazione" in c or "fine" in c.lower() or "Totale" in c][-1]
                col_var = [c for c in df.columns if "territorial" in c.lower()]
                for _, row in df.iterrows():
                    cod = str(row[col_cod]).strip().zfill(6)
                    val = pd.to_numeric(row[col_pop], errors="coerce")
                    if pd.notna(val):
                        salva_pop(cod, anno, val)
                    if col_var:
                        v_terr = pd.to_numeric(row[col_var], errors="coerce")
                        if pd.notna(v_terr) and v_terr != 0:
                            spostamenti[(cod, str(anno))] = int(round(v_terr))
                print(f"Anno {anno} caricato da {url}")
                break
            except Exception:
                continue

    return pop_storica, spostamenti

def ricostruisci():
    comuni_2026, variazioni, raccordo = carica_dati_situas()
    pop_storica, spostamenti_demo = carica_dati_demo_istat()

    comuni_2026["PRO_COM_T"] = comuni_2026["PRO_COM_T"].astype(str).str.strip().str.zfill(6)
    raccordo["PRO_COM_T_DT_IN"] = raccordo["PRO_COM_T_DT_IN"].astype(str).str.strip().str.zfill(6)
    raccordo["PRO_COM_T_DT_FI"] = raccordo["PRO_COM_T_DT_FI"].astype(str).str.strip().str.zfill(6)

    mappa_new_to_olds = raccordo.groupby("PRO_COM_T_DT_FI")["PRO_COM_T_DT_IN"].apply(lambda x: sorted(list(set(x)))).to_dict()

    serie_comuni, tipi_comuni, note_comuni = {}, {}, {}

    for _, row in comuni_2026.iterrows():
        cod_2026 = row["PRO_COM_T"]
        codici_origine = mappa_new_to_olds.get(cod_2026, [cod_2026])
        is_fusione = len(codici_origine) > 1

        serie_comuni[cod_2026], tipi_comuni[cod_2026], note_comuni[cod_2026] = {}, {}, []
        if is_fusione:
            note_comuni[cod_2026].append(f"Fusione (somma comuni originari: {', '.join(codici_origine)})")

        for anno in ANNI:
            if anno in pop_storica.get(cod_2026, {}) and not (is_fusione and int(anno) < 2019):
                serie_comuni[cod_2026][anno] = pop_storica[cod_2026][anno]
                tipi_comuni[cod_2026][anno] = "R"
            else:
                somma = sum(pop_storica.get(c_old, {}).get(anno, 0) for c_old in codici_origine)
                serie_comuni[cod_2026][anno] = somma
                tipi_comuni[cod_2026][anno] = "S" if (is_fusione and somma > 0) else "R"

    # Riproporzionamento arrotondato all'unità per scorpori/cessioni parziali (2019-2026)
    var_recenti = variazioni[pd.to_numeric(variazioni["ANNO"], errors="coerce") >= 2019].copy()
    for _, v in var_recenti.iterrows():
        desc = str(v.get("DESC_COD_VARIAZIONE", "")).lower()
        if any(k in desc for k in ["cessione", "distacco", "scorporo", "aggregazione parziale"]):
            anno_ev = str(int(v["ANNO"]))
            cod_c1 = str(v["PRO_COM_T"]).strip().zfill(6)
            cod_c2 = str(v["PRO_COM_T_REL"]).strip().zfill(6)

            delta_1 = spostamenti_demo.get((cod_c1, anno_ev), 0)
            if delta_1 < 0 and cod_c1 in serie_comuni and cod_c2 in serie_comuni:
                cedente, ricevente, delta_p = cod_c1, cod_c2, abs(delta_1)
                pop_ced_prec = serie_comuni[cedente].get(str(int(anno_ev) - 1), 0)
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

    risultati_json, risultati_csv = [], []
    for _, row in comuni_2026.iterrows():
        cod_2026 = row["PRO_COM_T"]
        denom = row["COMUNE"]
        sigla = row["SIGLA_AUTOMOBILISTICA"]
        nota_str = " | ".join(dict.fromkeys(note_comuni[cod_2026]))

        riga_csv = {"codice_istat": cod_2026, "denominazione": denom, "sigla_prov": sigla}
        for a in ANNI:
            riga_csv[a] = serie_comuni[cod_2026][a]
        riga_csv["anni_ricostruiti_somma"] = ",".join([a for a in ANNI if tipi_comuni[cod_2026][a] == "S"])
        riga_csv["anni_stimati_proporzione"] = ",".join([a for a in ANNI if tipi_comuni[cod_2026][a] == "P"])
        riga_csv["note_ricostruzione"] = nota_str

        risultati_json.append({
            "codice_istat": cod_2026,
            "denominazione": denom,
            "sigla_prov": sigla,
            "note": nota_str,
            "serie": {a: {"v": serie_comuni[cod_2026][a], "t": tipi_comuni[cod_2026][a]} for a in ANNI}
        })
        risultati_csv.append(riga_csv)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(risultati_csv).to_csv("data/popolazione_2026.csv", index=False, encoding="utf-8")
    with open("data/popolazione_2026.json", "w", encoding="utf-8") as f:
        json.dump(risultati_json, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Completato! Salvati {len(risultati_json)} comuni in data/popolazione_2026.json e .csv")

if __name__ == "__main__":
    ricostruisci()
