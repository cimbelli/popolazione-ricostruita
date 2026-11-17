import json
import os
import zipfile
import pandas as pd
import requests
from io import BytesIO

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

def leggi_csv_da_zip_remoto(url):
    r = requests.get(url, headers=HEADERS, timeout=180)
    r.raise_for_status()
    dfs = []
    with zipfile.ZipFile(BytesIO(r.content)) as z:
        for nome in sorted(z.namelist()):
            if nome.lower().endswith(".csv"):
                with z.open(nome) as f:
                    prime_righe = [f.readline().decode("utf-8", errors="replace") for _ in range(8)]
                    skip = 0
                    sep = ","
                    for idx, line in enumerate(prime_righe):
                        if any(k in line for k in ["Codice", "PRO_COM", "Comune", "Territorio", "Anno"]):
                            skip = idx
                            sep = ";" if line.count(";") >= line.count(",") else ","
                            break
                    f.seek(0)
                    df = pd.read_csv(f, sep=sep, skiprows=skip, encoding="utf-8", encoding_errors="replace", low_memory=False)
                    df.columns = [str(c).strip().replace('"', '') for c in df.columns]
                    dfs.append(df)
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

def trova_colonna(df, parole_chiave, esclusi=None):
    esclusi = esclusi or []
    for k in parole_chiave:
        for c in df.columns:
            cl = c.lower()
            if k.lower() in cl and not any(e.lower() in cl for e in esclusi):
                return c
    return None

def carica_dati_demo_istat():
    pop_storica = {}  # {cod_6_cifre: {anno: int}}
    spostamenti = {}  # {(cod_6_cifre, anno): int}

    def salva_pop(cod, anno, val):
        if pd.notna(val) and val > 0:
            pop_storica.setdefault(cod, {})[str(anno)] = int(round(val))

    # 1. Ricostruzione 2002-2019 (PopolazioneEta-Territorio-Comuni.zip)
    url_ric = "https://istat.it"
    print(f"Scaricamento RIC: {url_ric}")
    df_ric = leggi_csv_da_zip_remoto(url_ric)
    print("Colonne effettive RIC:", df_ric.columns.tolist())

    col_cod_ric = trova_colonna(df_ric, ["Codice", "PRO_COM", "ITTER"])
    col_eta = trova_colonna(df_ric, ["Età", "Eta"])
    if col_eta:
        df_ric = df_ric[df_ric[col_eta].astype(str).str.contains("Totale|999", case=False, na=True)]

    col_anno = trova_colonna(df_ric, ["Anno"])
    if col_anno:
        col_tot = trova_colonna(df_ric, ["Totale", "Popolazione", "Valore", "OBS_VALUE"], esclusi=["Codice", "Età", "Eta"])
        for _, row in df_ric.iterrows():
            cod = str(row[col_cod_ric]).strip().zfill(6)
            salva_pop(cod, int(float(row[col_anno])), pd.to_numeric(row[col_tot], errors="coerce"))
    else:
        for _, row in df_ric.iterrows():
            cod = str(row[col_cod_ric]).strip().zfill(6)
            for a in range(2002, 2020):
                if str(a) in df_ric.columns:
                    salva_pop(cod, a, pd.to_numeric(row[str(a)], errors="coerce"))

    # 2. Bilanci demografici D7B (2019-2026)
    for anno in range(2019, 2027):
        url_d7b = f"https://istat.it{anno}.csv.zip"
        if requests.head(url_d7b, headers=HEADERS, timeout=30).status_code != 200:
            continue

        df_b = leggi_csv_da_zip_remoto(url_d7b)
        if anno == 2019:
            print("Colonne effettive D7B:", df_b.columns.tolist())

        col_cod = trova_colonna(df_b, ["Codice Comune", "Codice", "PRO_COM"])
        col_mese = trova_colonna(df_b, ["Mese"])
        col_var = trova_colonna(df_b, ["variazioni territoriali", "territorial"])
        col_pop = trova_colonna(df_b, ["Popolazione fine periodo", "fine periodo", "Popolazione", "Totale"], esclusi=["Codice", "inizio"])

        df_b["_COD"] = df_b[col_cod].astype(str).str.strip().str.zfill(6)

        if col_var:
            var_annua = df_b.groupby("_COD")[col_var].apply(lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum())
            for cod, v_sum in var_annua.items():
                if v_sum != 0:
                    spostamenti[(cod, str(anno))] = int(round(v_sum))

        if col_mese:
            df_b[col_mese] = pd.to_numeric(df_b[col_mese], errors="coerce")
            df_b = df_b.sort_values(col_mese).groupby("_COD", as_index=False).last()

        for _, row in df_b.iterrows():
            salva_pop(row["_COD"], anno, pd.to_numeric(row[col_pop], errors="coerce"))

    roma_2002 = pop_storica.get("058091", {}).get("2002", 0)
    roma_2025 = pop_storica.get("058091", {}).get("2025", 0)
    print(f"VERIFICA ROMA (058091): 2002 = {roma_2002} | 2025 = {roma_2025}")
    if roma_2002 == 0 or roma_2025 == 0:
        raise RuntimeError(f"ERRORE: Dati incompleti per Roma (2002={roma_2002}, 2025={roma_2025}). Leggi le colonne stampate sopra!")

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

    # Riproporzionamento arrotondato all'unità a somma zero per scorpori/cessioni parziali (2019-2026)
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
            "c": cod_2026,
            "n": denom,
            "p": sigla,
            "m": nota_str,
            "v": [serie_comuni[cod_2026][a] for a in ANNI],
            "t": "".join([tipi_comuni[cod_2026][a] for a in ANNI])
        })
        risultati_csv.append(riga_csv)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(risultati_csv).to_csv("data/popolazione_2026.csv", index=False, encoding="utf-8")
    with open("data/popolazione_2026.json", "w", encoding="utf-8") as f:
        json.dump(risultati_json, f, ensure_ascii=False, separators=(",", ":"))

