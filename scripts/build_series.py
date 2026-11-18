"""
Serie storica della popolazione residente al 1° gennaio, 2002-ultimo anno,
ricostruita ai confini comunali vigenti (fonte: Istat demo + SITUAS).

Fonti
-----
* 2002-2018: Ricostruzione intercensuaria della popolazione (demo.istat.it, i=RIC).
  Istat la diffonde con la classificazione dei comuni vigente nel 2019.
  Ripiego automatico: file "RCS" (popolazione per cittadinanza), che per il
  2002-2018 provengono dalla stessa ricostruzione.
* 2019-ultimo anno: POSAS (popolazione per età, sesso e stato civile al 1/1),
  ogni anno con i comuni vigenti in quell'anno.
* Raccordo territoriale: SITUAS report 99 "Traslazione Comuni Data inizio – Data
  fine", interrogato per ogni anno (01/01/AAAA -> fine validità). Ogni codice
  comunale di un anno viene così portato al codice vigente oggi.
* Elenco dei comuni vigenti: SITUAS report 61. Note sulle variazioni: report 129.

Regole di ricostruzione (colonna "t" / tipo per anno)
----------------------------------------------------
R = dato Istat del comune, invariato (relazione 1:1)
S = somma di più comuni di allora confluiti nel comune attuale (fusioni/incorporazioni)
P = quota stimata: un comune di allora è stato diviso tra più comuni attuali senza
    sopravvivere; la sua popolazione è ripartita in proporzione alla popolazione
    più recente dei comuni di destinazione
- = dato mancante

Lo script si ferma con errore se i totali nazionali non sono plausibili: meglio
un'Action rossa che un JSON pieno di zeri.

Variabili d'ambiente opzionali: RIC_URL (forza l'URL del file comuni della
ricostruzione), ANNO_FINE (ultimo anno da cercare, default anno corrente + 1).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import defaultdict
from datetime import date
from urllib.parse import urljoin

import pandas as pd
import requests

DEMO = "https://demo.istat.it"
POSAS_URL = DEMO + "/data/posas/POSAS_{anno}_it_Comuni.zip"
RCS_URL = DEMO + "/data/rcs/Dati_RCS_cittadinanza_{anno}.zip"
RIC_PAGES = [DEMO + "/app/?i=RIC&l=it", DEMO + "/app/?i=RIC&l=en"]

ANNO_INIZIO = 2002
ANNO_FINE = int(os.environ.get("ANNO_FINE", date.today().year + 1))
ANNO_CENSIMENTO = 2019  # da qui in poi POSAS

OUT_DIR = "data"
RAW_DIR = os.path.join(OUT_DIR, "raw")
HEADERS = {"User-Agent": "Mozilla/5.0 (popolazione-ricostruita; +https://github.com/cimbelli/popolazione-ricostruita)"}

session = requests.Session()
session.headers.update(HEADERS)


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------
# Download e lettura CSV Istat (robusta su separatore, encoding, righe di titolo)
# --------------------------------------------------------------------------
def scarica(url: str, tentativi: int = 3) -> bytes | None:
    for i in range(tentativi):
        try:
            r = session.get(url, timeout=300)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            log(f"  tentativo {i + 1} fallito per {url}: {e}")
            time.sleep(5 * (i + 1))
    return None


def _decodifica(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def leggi_csv(raw: bytes) -> pd.DataFrame:
    """Legge un CSV Istat saltando le eventuali righe di titolo iniziali."""
    testo = _decodifica(raw)
    righe = testo.splitlines()
    start = 0
    for i, r in enumerate(righe[:15]):
        rl = r.lower()
        if ("codice" in rl or "territorio" in rl or "itter" in rl) and (";" in r or "," in r or "\t" in r):
            start = i
            break
    header = righe[start]
    sep = max([";", ",", "\t", "|"], key=header.count)
    df = pd.read_csv(io.StringIO("\n".join(righe[start:])), sep=sep, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip().strip('"').strip() for c in df.columns]
    return df


def csv_da_zip(raw: bytes) -> list[tuple[str, pd.DataFrame]]:
    """Tutti i CSV contenuti nello zip (anche in zip annidati)."""
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for nome in sorted(z.namelist()):
            nl = nome.lower()
            if nl.endswith(".zip"):
                out.extend(csv_da_zip(z.read(nome)))
            elif nl.endswith(".csv") or nl.endswith(".txt"):
                out.append((nome, leggi_csv(z.read(nome))))
    return out


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def col(df: pd.DataFrame, *chiavi: str, escludi: tuple[str, ...] = ()) -> str | None:
    for k in chiavi:
        for c in df.columns:
            n = norm(c)
            if k in n and not any(e in n for e in escludi):
                return c
    return None


def a_numero(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False).str.strip(),
                         errors="coerce").fillna(0)


def codice6(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)


def colonne_totale(cols: list[str]) -> list[str]:
    """Colonne da sommare per avere il totale (M+F) tra quelle date."""
    n = {c: norm(c) for c in cols}
    tot = [c for c in cols if "total" in n[c] and not any(k in n[c] for k in ("masch", "femm", "male", "fem"))]
    if tot:
        return tot[:1]
    tot_mf = [c for c in cols if "total" in n[c] and any(k in n[c] for k in ("masch", "femm", "male", "fem"))]
    if len(tot_mf) >= 2:
        return tot_mf
    mf = [c for c in cols if n[c] in ("maschi", "femmine", "males", "females", "m", "f")]
    if len(mf) == 2:
        return mf
    return []


def filtra_eta_totale(df: pd.DataFrame) -> pd.DataFrame:
    """Tiene la riga del totale per età (999/Totale) se esiste, altrimenti tutte le età (da sommare)."""
    c_eta = col(df, "eta", "age", escludi=("class",))
    if not c_eta:
        return df
    v = df[c_eta].astype(str).str.strip().str.lower()
    tot = v.isin(["999", "totale", "total", "tutte le eta", "tutte le età"])
    return df[tot] if tot.any() else df


def popolazione_da_df(df: pd.DataFrame, anno: int | None = None) -> dict[str, dict[int, int]]:
    """Restituisce {codice: {anno: popolazione}} da un CSV demo (formato largo o lungo)."""
    c_cod = col(df, "codice comune", "pro_com", "cod_com", "codice", "itter")
    if not c_cod:
        raise ValueError(f"colonna codice comune non trovata: {list(df.columns)}")
    df = filtra_eta_totale(df.copy())
    # file per cittadinanza/paese: se c'è una riga di totale usa solo quella
    c_citt = col(df, "cittadinanza", "paese", "citizenship", "country")
    if c_citt:
        v = df[c_citt].astype(str).map(norm)
        tot = v.isin(["totale", "total", "mondo", "world", "tutte", "tutti i paesi"])
        if tot.any():
            df = df[tot]
    df["_COD"] = codice6(df[c_cod])
    df = df[df["_COD"].str.fullmatch(r"\d{6}")]
    out: dict[str, dict[int, int]] = defaultdict(dict)

    c_anno = col(df, "anno", "year", "time")
    anni_in_colonne = sorted({int(m) for c in df.columns for m in re.findall(r"(?<!\d)(20\d\d)(?!\d)", c)})

    if c_anno or anno is not None:
        cols = colonne_totale([c for c in df.columns if c not in (c_cod, "_COD")])
        if not cols:
            c_val = col(df, "value", "valore", "obs_value", "popolazione", "residenti")
            cols = [c_val] if c_val else []
        if not cols:
            raise ValueError(f"colonna totale non trovata: {list(df.columns)}")
        df["_V"] = sum(a_numero(df[c]) for c in cols)
        df["_A"] = pd.to_numeric(df[c_anno], errors="coerce") if c_anno else anno
        g = df.groupby(["_COD", "_A"])["_V"].sum()
        for (cod, a), v in g.items():
            if pd.notna(a):
                out[cod][int(a)] = int(round(v))
    elif anni_in_colonne:
        for a in anni_in_colonne:
            cols_a = [c for c in df.columns if str(a) in c]
            cols = colonne_totale(cols_a) or (cols_a if len(cols_a) == 1 else [])
            if not cols:
                continue
            v = sum(a_numero(df[c]) for c in cols)
            for cod, val in v.groupby(df["_COD"]).sum().items():
                out[cod][a] = int(round(val))
    else:
        raise ValueError(f"formato non riconosciuto: {list(df.columns)}")
    return out


def unisci(dst, src, anni_validi):
    for cod, serie in src.items():
        for a, v in serie.items():
            if a in anni_validi:
                dst[cod][a] = dst[cod].get(a, 0) + v


# --------------------------------------------------------------------------
# Fonti demo
# --------------------------------------------------------------------------
def trova_url_ricostruzione() -> list[str]:
    if os.environ.get("RIC_URL"):
        return [os.environ["RIC_URL"]]
    trovati = []
    for pagina in RIC_PAGES:
        try:
            html = session.get(pagina, timeout=60).text
        except requests.RequestException as e:
            log(f"  pagina {pagina} non raggiungibile: {e}")
            continue
        links = re.findall(r"""["']([^"'\s]+?\.zip)["']""", html)
        links = [urljoin(pagina, l) for l in links]
        log(f"  link .zip trovati in {pagina}: {len(links)}")
        for l in links:
            log(f"    {l}")
        trovati += links
    trovati = list(dict.fromkeys(trovati))
    # preferisci il file unico "Comuni", poi i file per provincia della ricostruzione
    comuni = [l for l in trovati if re.search(r"comun|municip", l, re.I) and not re.search(r"bilanc|rbd", l, re.I)]
    return comuni


def carica_ricostruzione(anni_validi: set[int]) -> tuple[dict, str]:
    pop: dict[str, dict[int, int]] = defaultdict(dict)
    urls = trova_url_ricostruzione()
    for url in urls:
        log(f"RIC: scarico {url}")
        raw = scarica(url)
        if not raw:
            continue
        try:
            for nome, df in csv_da_zip(raw):
                log(f"  {nome}: colonne {list(df.columns)[:12]}{' ...' if len(df.columns) > 12 else ''}")
                unisci(pop, popolazione_da_df(df), anni_validi)
        except Exception as e:  # formato inatteso: prova il link successivo
            log(f"  lettura fallita: {e}")
            pop = defaultdict(dict)
            continue
        if pop:
            return pop, f"Ricostruzione intercensuaria ({url})"

    log("RIC non disponibile: ripiego sui file RCS (cittadinanza) per anno")
    for anno in sorted(anni_validi):
        url = RCS_URL.format(anno=anno)
        raw = scarica(url)
        if not raw:
            log(f"  RCS {anno}: non trovato")
            continue
        for nome, df in csv_da_zip(raw):
            if anno == min(anni_validi):
                log(f"  {nome}: colonne {list(df.columns)}")
            # i file RCS hanno una riga per comune x cittadinanza: si somma tutto
            unisci(pop, popolazione_da_df(df, anno=anno), {anno})
    return pop, "Popolazione per cittadinanza RCS (dalla ricostruzione 2002-2018)"


def carica_posas(anni: list[int]) -> tuple[dict, list[int]]:
    pop: dict[str, dict[int, int]] = defaultdict(dict)
    disponibili = []
    for anno in anni:
        url = POSAS_URL.format(anno=anno)
        raw = scarica(url)
        if not raw:
            log(f"POSAS {anno}: non disponibile")
            continue
        for nome, df in csv_da_zip(raw):
            if not disponibili:
                log(f"POSAS {anno} {nome}: colonne {list(df.columns)}")
            unisci(pop, popolazione_da_df(df, anno=anno), {anno})
        disponibili.append(anno)
        log(f"POSAS {anno}: ok")
    return pop, disponibili


# --------------------------------------------------------------------------
# SITUAS
# --------------------------------------------------------------------------
def situas(pfun: int, **kw) -> pd.DataFrame:
    import opensituas as osi
    for i in range(3):
        try:
            df = osi.get(pfun, **kw)
            df.columns = [str(c).strip() for c in df.columns]
            return df
        except Exception as e:
            log(f"  SITUAS {pfun} {kw} tentativo {i + 1}: {e}")
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"SITUAS report {pfun} non disponibile")


def fine_validita(pfun: int) -> str:
    from opensituas import _catalog
    return _catalog.validity_end(_catalog.get_entry(pfun))


def comuni_vigenti() -> pd.DataFrame:
    df = situas(61)
    log(f"SITUAS 61 colonne: {list(df.columns)}")
    c_cod = col(df, "pro_com_t") or col(df, "pro_com")
    c_nom = next((c for c in df.columns if norm(c) in ("comune", "denominazione", "denominazione italiana")), None) \
        or col(df, "comune", "denominaz", escludi=("pro_com", "cod", "alt"))
    c_sig = col(df, "sigla")
    c_reg = next((c for c in df.columns if norm(c) in ("den_reg", "regione", "denominazione regione")), None) or col(df, "den_reg", "regione", escludi=("cod",))
    out = pd.DataFrame({
        "codice": codice6(df[c_cod]),
        "nome": df[c_nom].astype(str).str.strip(),
        "sigla": df[c_sig].astype(str).str.strip() if c_sig else "",
        "regione": df[c_reg].astype(str).str.strip() if c_reg else "",
    }).drop_duplicates("codice").sort_values("codice").reset_index(drop=True)
    return out


def raccordo(anno: int, data_fine: str) -> dict[str, set[str]]:
    """{codice valido al 01/01/anno: {codici vigenti alla data_fine}} dal report 99."""
    df = situas(99, date_from=f"01/01/{anno}", date_to=data_fine)
    if df.empty:
        return {}
    cod_cols = [c for c in df.columns if "pro_com" in norm(c)]
    pref_t = [c for c in cod_cols if norm(c).startswith("pro_com_t")] or cod_cols

    def punteggio(c: str, chiavi: tuple[str, ...]) -> int:
        n = norm(c)
        return sum(k in n for k in chiavi)

    ini = ("_in", "ini", "inizio", "_da", "orig", "prec", "_i_")
    fin = ("_fi", "fin", "fine", "dest", "succ", "attual", "_f_")
    c_in = max(pref_t, key=lambda c: punteggio(c, ini) - punteggio(c, fin), default=None)
    c_fi = max(pref_t, key=lambda c: punteggio(c, fin) - punteggio(c, ini), default=None)
    if c_in == c_fi and len(pref_t) == 2:
        c_in, c_fi = pref_t
    if not (c_in and c_fi) or c_in == c_fi:
        raise RuntimeError(f"SITUAS 99: colonne codice non riconosciute: {list(df.columns)}")
    if anno == ANNO_INIZIO:
        log(f"SITUAS 99 colonne: {list(df.columns)} -> origine={c_in}, destinazione={c_fi}")
    m: dict[str, set[str]] = defaultdict(set)
    for a, b in zip(codice6(df[c_in]), codice6(df[c_fi])):
        if re.fullmatch(r"\d{6}", a) and re.fullmatch(r"\d{6}", b):
            m[a].add(b)
    return m


def variazioni_note(vigenti: set[str]) -> dict[str, list[str]]:
    """Note leggibili sulle variazioni dal 2002 (report 129). Facoltativo."""
    note: dict[str, list[str]] = defaultdict(list)
    try:
        df = situas(129)
    except Exception as e:
        log(f"SITUAS 129 non disponibile, niente note: {e}")
        return note
    log(f"SITUAS 129 colonne: {list(df.columns)}")
    c_anno = col(df, "anno")
    c_data = col(df, "data_decorrenza", "data decorrenza", "decorrenza", "data")
    c_desc = col(df, "desc", "tipo_variazione", "tipo variazione")
    c_cod = col(df, "pro_com_t", escludi=("rel", "succ", "orig"))
    c_rel = col(df, "pro_com_t_rel", "rel", "succ")
    c_nom = col(df, "comune", "denominaz", escludi=("pro_com", "rel", "succ"))
    c_nom_rel = col(df, "comune_rel", "denominazione_rel", "comune rel")
    if not (c_cod and (c_anno or c_data)):
        log("SITUAS 129: colonne non riconosciute, niente note")
        return note
    for _, r in df.iterrows():
        anno = None
        if c_anno:
            anno = pd.to_numeric(r[c_anno], errors="coerce")
        if (anno is None or pd.isna(anno)) and c_data:
            m = re.search(r"(19|20)\d\d", str(r[c_data]))
            anno = int(m.group(0)) if m else None
        if anno is None or pd.isna(anno) or int(anno) < ANNO_INIZIO:
            continue
        desc = str(r[c_desc]).strip() if c_desc else "variazione"
        a = str(r[c_cod]).strip().zfill(6)
        b = str(r[c_rel]).strip().zfill(6) if c_rel and str(r[c_rel]).strip() not in ("", "nan") else ""
        nb = str(r[c_nom_rel]).strip() if c_nom_rel else b
        na = str(r[c_nom]).strip() if c_nom else a
        if a in vigenti:
            note[a].append(f"{int(anno)}: {desc}" + (f" (con {nb})" if b else ""))
        if b in vigenti and b != a:
            note[b].append(f"{int(anno)}: {desc} (con {na})")
    return note


# --------------------------------------------------------------------------
# Ricostruzione
# --------------------------------------------------------------------------
def ricostruisci():
    os.makedirs(RAW_DIR, exist_ok=True)
    anni_cercati = list(range(ANNO_INIZIO, ANNO_FINE + 1))

    # 1. Dati demo
    posas, anni_posas = carica_posas([a for a in anni_cercati if a >= ANNO_CENSIMENTO])
    if not anni_posas:
        raise RuntimeError("Nessun file POSAS scaricato: controlla gli URL in POSAS_URL")
    anni_ric = set(range(ANNO_INIZIO, ANNO_CENSIMENTO + 1))  # 2019 serve solo come controllo
    ric, fonte_ric = carica_ricostruzione(anni_ric)
    anni = list(range(ANNO_INIZIO, max(anni_posas) + 1))

    # dati per anno: {anno: {codice_allora: pop}}
    dati: dict[int, dict[str, int]] = defaultdict(dict)
    for cod, s in ric.items():
        for a, v in s.items():
            if a < ANNO_CENSIMENTO:
                dati[a][cod] = v
    for cod, s in posas.items():
        for a, v in s.items():
            dati[a][cod] = v

    # 2. Controllo di plausibilità sui totali nazionali (Italia 57-60 milioni)
    totali = {a: sum(dati[a].values()) for a in anni}
    log("Totali nazionali per anno (dati originali):")
    for a in anni:
        log(f"  {a}: {totali[a]:>12,}  comuni: {len(dati[a])}")
    controllo_2019 = sum(v.get(2019, 0) for v in ric.values())
    if controllo_2019:
        log(f"Controllo 2019: ricostruzione {controllo_2019:,} vs POSAS {totali.get(2019, 0):,}")
    mancanti = [a for a in anni if not (50_000_000 < totali[a] < 65_000_000)]
    if mancanti:
        raise RuntimeError(f"Totali nazionali non plausibili per gli anni {mancanti}. Leggi le colonne stampate sopra.")

    # 3. Comuni vigenti e raccordi SITUAS anno per anno
    vig = comuni_vigenti()
    vigenti = set(vig["codice"])
    log(f"Comuni vigenti: {len(vigenti)}")
    data_fine = fine_validita(99)
    log(f"Raccordo SITUAS verso il {data_fine}")
    raccordi = {a: raccordo(a, data_fine) for a in anni}
    unione: dict[str, set[str]] = defaultdict(set)
    for m in raccordi.values():
        for k, v in m.items():
            unione[k] |= v

    def destinazioni(cod: str, anno: int) -> set[str]:
        for m in ([raccordi[ANNO_CENSIMENTO], raccordi[anno]] if anno < ANNO_CENSIMENTO else [raccordi[anno]]) + [unione]:
            if cod in m:
                return m[cod]
        return {cod} if cod in vigenti else set()

    # peso per le divisioni: popolazione più recente del comune di destinazione
    ultimo = max(anni)
    peso = {c: dati[ultimo].get(c, 0) for c in vigenti}

    serie = {c: {} for c in vigenti}
    fonti = {c: {} for c in vigenti}   # {anno: set(codici allora)}
    tipo = {c: {} for c in vigenti}
    non_raccordati = defaultdict(list)
    for a in anni:
        for cod, v in dati[a].items():
            dest = destinazioni(cod, a) & vigenti
            if not dest:
                non_raccordati[a].append(cod)
                continue
            if cod in dest and len(dest) > 1:
                # il comune esiste ancora e ha ceduto solo una parte: resta tutto a lui
                quote = {cod: v}
            elif len(dest) == 1:
                quote = {next(iter(dest)): v}
            else:
                tot_w = sum(peso[d] for d in dest) or len(dest)
                quote = {d: v * (peso[d] or 1) / tot_w for d in dest}
            for d, q in quote.items():
                serie[d][a] = serie[d].get(a, 0) + q
                fonti[d].setdefault(a, set()).add(cod)
                if len(quote) > 1:
                    tipo[d][a] = "P"
    for c in vigenti:
        for a in anni:
            if a not in serie[c]:
                tipo[c][a] = "-"
            elif tipo[c].get(a) != "P":
                tipo[c][a] = "S" if len(fonti[c][a]) > 1 else "R"
            if a in serie[c]:
                serie[c][a] = int(round(serie[c][a]))

    for a in anni:
        if non_raccordati[a]:
            persi = sum(dati[a][c] for c in non_raccordati[a])
            log(f"ATTENZIONE {a}: {len(non_raccordati[a])} codici non raccordati ({persi:,} ab.): {non_raccordati[a][:20]}")

    tot_rico = {a: sum(serie[c].get(a, 0) for c in vigenti) for a in anni}
    log("Totali ricostruiti ai confini vigenti:")
    for a in anni:
        diff = tot_rico[a] - totali[a]
        log(f"  {a}: {tot_rico[a]:>12,}  (scarto {diff:+,})")
        if abs(diff) > 0.001 * totali[a]:
            raise RuntimeError(f"Scarto eccessivo nel {a}: controlla il raccordo SITUAS")

    roma = serie.get("058091", {})
    log(f"VERIFICA ROMA 058091: 2002={roma.get(2002)} 2019={roma.get(2019)} {ultimo}={roma.get(ultimo)}")

    # 4. Note
    note = variazioni_note(vigenti)
    for c in vigenti:
        orig = sorted({o for a in anni for o in fonti[c].get(a, set())} - {c})
        if orig:
            note[c].insert(0, "Comprende i codici storici " + ", ".join(orig))

    # 5. Output
    vig = vig.set_index("codice")
    righe_csv, comuni_json = [], []
    for c in sorted(vigenti):
        r = vig.loc[c]
        valori = [serie[c].get(a) for a in anni]
        t = "".join(tipo[c][a] for a in anni)
        nota = " | ".join(dict.fromkeys(note.get(c, [])))
        comuni_json.append([c, r["nome"], r["sigla"], valori, t if set(t) != {"R"} else "", nota])
        riga = {"codice_istat": c, "comune": r["nome"], "sigla_prov": r["sigla"], "regione": r["regione"]}
        riga.update({str(a): v for a, v in zip(anni, valori)})
        riga["anni_somma_fusione"] = ",".join(str(a) for a in anni if tipo[c][a] == "S")
        riga["anni_stima_proporzionale"] = ",".join(str(a) for a in anni if tipo[c][a] == "P")
        riga["note"] = nota
        righe_csv.append(riga)

    meta = {
        "aggiornato": date.today().isoformat(),
        "confini_al": data_fine,
        "anni": anni,
        "fonti": {
            f"{ANNO_INIZIO}-{ANNO_CENSIMENTO - 1}": fonte_ric,
            f"{ANNO_CENSIMENTO}-{max(anni)}": "POSAS, popolazione residente al 1° gennaio (demo.istat.it)",
            "raccordo": "SITUAS report 99, 61, 129",
        },
        "totali_italia": {str(a): tot_rico[a] for a in anni},
        "campi": ["codice", "comune", "sigla", "valori", "tipi (vuoto = tutti R)", "note"],
    }
    pd.DataFrame(righe_csv).to_csv(os.path.join(OUT_DIR, "popolazione_2026.csv"), index=False, encoding="utf-8")
    with open(os.path.join(OUT_DIR, "popolazione_2026.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "comuni": comuni_json}, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Scritti {len(comuni_json)} comuni, anni {anni[0]}-{anni[-1]}")


if __name__ == "__main__":
    try:
        ricostruisci()
    except Exception as e:
        log(f"ERRORE: {e}")
        sys.exit(1)
