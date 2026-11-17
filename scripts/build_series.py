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
            if nome.endswith(".csv"):
                with z.open(nome) as f:
                    # Leggi saltando eventuali righe di intestazione descrittiva prima del header reale
                    raw_lines = [f.readline().decode("latin1") for _ in range(5)]
                    skip = 0
                    for idx, line in enumerate(raw_lines):
                        if ";" in line and ("Codice" in line or "PRO_COM" in line or "Anno" in line or "Comune" in line):
                            skip = idx
                            break
                    f.seek(0)
                    df = pd.read_csv(f, sep=";", encoding="latin1", skiprows=skip, low_memory=False)
                    dfs.append(df)
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

def carica_dati_demo_istat():
    pop_storica = {}  # {cod_6_cifre: {anno: int}}
    spostamenti = {}  # {(cod_6_cifre, anno): int}

    def salva_pop(cod, anno, val):
        if pd.notna(val) and val > 0:
            pop_storica.setdefault(cod, {})[str(anno)] = int(round(val))

    # 1. Ricostruzione 2002-2019 (file unico nazionale per tutti i comuni ai confini 2019)
    url_ric = "https://demo.istat.it/data/ricostruzione/PopolazioneEta-Territorio-Comuni.zip"
    print(f"Scaricamento ricostruzione 2002-2019: {url_ric}")
    df_ric = leggi_csv_da_zip_remoto(url_ric)
    print(f"Colonne RIC: {df_ric.columns.tolist()[:15]}")

    col_cod_ric = [c for c in df_ric.columns if "Codice" in c or "PRO_COM" in c][0]
    # Filtra solo il totale per età se presente la colonna Età
    col_eta = [c for c in df_ric.columns if "Et" in c and len(c) <= 5]
    if col_eta:
        df_ric = df_ric[df_ric[col_eta[0]].astype(str).str.contains("Totale|999", na=True)]

    if "Anno" in df_ric.columns:
        col_tot = [c for c in df_ric.columns if "Totale" in c or "Popolazione" in c][-1]
        for _, row in df_ric.iterrows():
            cod = str(row[col_cod_ric]).strip().zfill(6)
            salva_pop(cod, int(row["Anno"]), pd.to_numeric(row[col_tot], errors="coerce"))
    else:
        for _, row in df_ric.iterrows():
            cod = str(row[col_cod_ric]).strip().zfill(6)
            for a in range(2002, 2020):
                if str(a) in df_ric.columns:
                    salva_pop(cod, a, pd.to_numeric(row[str(a)], errors="coerce"))

    # 2. Bilanci demografici nazionali D7B (2019-2026) con popolazione e variazioni territoriali
    for anno in range(2019, 2027):
        url_d7b = f"https://demo.istat.it/data/d7b/D7B{anno}.csv.zip"
        r_check = requests.head(url_d7b, headers=HEADERS, timeout=30)
        if r_check.status_code != 200:
            print(f"Info: {url_d7b} non ancora pubblicato (status {r_check.status_code})")
            continue

        print(f"Scaricamento bilancio {anno}: {url_d7b}")
        df_b = leggi_csv_da_zip_remoto(url_d7b)
        if anno == 2019:
            print(f"Colonne D7B: {df_b.columns.tolist()}")

        col_cod = [c for c in df_b.columns if "Codice" in c or "PRO_COM" in c][0]
        # Nei file D7B mensili/annuali prendiamo l'ultimo mese disponibile (es. Dicembre o max Mese)
        col_mese = [c for c in df_b.columns if "Mese" in c]
        if col_mese:
            df_b[col_mese[0]] = pd.to_numeric(df_b[col_mese[0]], errors="coerce")
            # Somma le variazioni territoriali su tutti i mesi dell'anno
            col_var = [c for c in df_b.columns if "territorial" in c.lower()]
            if col_var:
                var_annua = df_b.groupby(df_b[col_cod].astype(str).str.strip().str.zfill(6))[col_var[0]].apply(
                    lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()
                )
                for cod, v_sum in var_annua.items():
                    if v_sum != 0:
                        spostamenti[(cod, str(anno))] = int(round(v_sum))

            # Per la popolazione residente prendi l'ultimo mese disponibile nell'anno per ciascun comune
            df_b = df_b.sort_values(col_mese[0]).groupby(col_cod, as_index=False).last()

        col_pop = [c for c in df_b.columns if "fine" in c.lower() or "Popolazione" in c or "Totale" in c][-1]
        for _, row in df_b.iterrows():
            cod = str(row[col_cod]).strip().zfill(6)
            salva_pop(cod, anno, pd.to_numeric(row[col_pop], errors="coerce"))

    # Controllo di sicurezza anti-zero (verifica Roma = 058091)
    roma_2002 = pop_storica.get("058091", {}).get("2002", 0)
    roma_2025 = pop_storica.get("058091", {}).get("2025", 0)
    print(f"Verifica Roma (058091): 2002 = {roma_2002:,} | 2025 = {roma_2025:,}")
    if roma_2002 == 0:
        raise RuntimeError("BLOCCO DI SICUREZZA: Roma 2002 risulta ancora 0, controlla nomi colonne nei log.")

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
            "codice_istat": cod_2026,
            "denominazione": denom,
            "sigla_prov": sigla,
            "note": nota_str,
            "serie": {a: {"v": serie_comuni[cod_2026][a], "t": tipi_comuni[cod_2026][a]} for a in ANNI}
        })
        risultati_csv.append(riga_csv)

    os.makedirs("data", exist_ok=True)
    pd.DataFrame(risultati_csv).to_csv("data/popolazione_2026.csv", index=False, encoding="utf-8")
