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

import shutil

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
    """Legge un CSV Istat saltando le righe di titolo iniziali.

    L'intestazione è la prima riga con almeno 3 campi seguita da una riga con lo stesso
    numero di campi; le righe di titolo vengono conservate in df.attrs["titolo"]."""
    testo = _decodifica(raw)
    righe = testo.splitlines()
    best = None  # (indice riga, separatore)
    for sep in (";", ",", "\t", "|"):
        for i in range(min(30, len(righe) - 1)):
            n = len(next(csv.reader([righe[i]], delimiter=sep)))
            if n >= 3 and len(next(csv.reader([righe[i + 1]], delimiter=sep))) == n:
                if best is None or i < best[0] or (i == best[0] and n > best[2]):
                    best = (i, sep, n)
                break
    start, sep = (best[0], best[1]) if best else (0, ";")
    df = pd.read_csv(io.StringIO("\n".join(righe[start:])), sep=sep, dtype=str, keep_default_na=False,
                     on_bad_lines="skip")
    df.columns = [str(c).strip().strip('"').strip() for c in df.columns]
    df.attrs["titolo"] = " ".join(righe[:start])
    df.attrs["anteprima"] = righe[:start + 3]
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
                df = leggi_csv(z.read(nome))
                df.attrs["file"] = nome
                out.append((nome, df))
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
    return pd.to_numeric(s.fillna("").astype(object).map(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False).str.strip(),
                         errors="coerce").fillna(0)


def codice6(s: pd.Series) -> pd.Series:
    # fillna: con pandas 3 i valori mancanti restano NaN anche dopo astype(str)
    x = s.fillna("").astype(object).map(str).str.strip().str.replace(r"\.0$", "", regex=True)
    x = x.where(~x.str.lower().isin(["", "nan", "none", "<na>"]), "")
    return x.where(x == "", x.str.zfill(6))


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
    c_cod = col(df, "codice comune", "codice istat", "pro_com", "cod_com", "cod. comune", "itter",
                escludi=("stato", "cittadin", "paese", "provinc", "region"))
    if not c_cod:
        c_cod = col(df, "codice", escludi=("stato", "cittadin", "paese", "provinc", "region", "eta"))
    df = df.copy()
    if not c_cod:
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", df.attrs.get("titolo", "") + " " + df.attrs.get("file", ""))
        if not m:
            raise ValueError(f"colonna codice comune non trovata: {list(df.columns)}")
        c_cod = "_CODTITOLO"
        df[c_cod] = m.group(1)
    df = filtra_eta_totale(df)
    # file per cittadinanza/paese: contengono Paesi + subtotali (zona, continente, mondo).
    # Si tiene solo la riga del totale generale.
    desc = [c for c in df.columns if any(k in norm(c) for k in ("cittadinanza", "paese", "zona", "continente", "country", "citizenship"))
            and "codice" not in norm(c)]
    per_paese = bool(desc)
    # (nessun filtro: più sotto si prende il massimo per comune e anno = totale generale)
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
        g = df.groupby(["_COD", "_A"])["_V"].max() if desc else df.groupby(["_COD", "_A"])["_V"].sum()
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
    return sorted(comuni, key=lambda l: 0 if "singolaarea" in l.lower() else 1)



# --------------------------------------------------------------------------
# Lettore dei file della ricostruzione (demo.istat.it/data/ricostruzione/)
# Non sono CSV semplici: righe di titolo, doppia intestazione, età in riga.
# --------------------------------------------------------------------------
def _campi(riga: str) -> list[str]:
    return [c.strip().strip('"').strip() for c in next(csv.reader([riga], delimiter=";"))]


def _num(x: str) -> int | None:
    x = x.replace(".", "").replace(" ", "")
    return int(x) if re.fullmatch(r"-?\d+", x) else None


def _cittadinanza(riga: str) -> str | None:
    n = norm(riga)
    if "tutte le cittadinanze" in n or "totale cittadinanze" in n:
        return "tutte"
    if "stranier" in n or "italian" in n or "cittadinanza" in n:
        return "altro"
    return None


def _sesso(etichetta: str) -> str | None:
    n = norm(etichetta)
    if n.startswith("total") or ("masch" in n and "femm" in n):
        return "totale"
    if n.startswith("masch") or n.startswith("femm"):
        return "altro"
    return None


def leggi_ric_singola_area(testo: str, nome: str, blocchi: list | None = None) -> dict[str, dict[int, int]]:
    """Un file per comune, fatto di più blocchi (tutte le cittadinanze / stranieri, totale / maschi / femmine).
    Ogni blocco: titolo, 'Età/Anno;2002;...;2019', 'Età;<sesso>', una riga per età, di norma 'Totale'.
    Si tiene solo il blocco 'Tutte le cittadinanze' + 'Totale'."""
    m = re.search(r"Comune:\s*(\d{6})", testo) or re.search(r"(?<!\d)(\d{6})(?!\d)", nome)
    if not m:
        raise ValueError(f"codice comune non trovato in {nome}")
    cod = m.group(1)
    citt, sesso, anni = "tutte", None, None
    somma, totale, trovato = None, None, None
    citt_blocco, sesso_blocco = "tutte", None
    for riga in testo.splitlines():
        if not riga.strip():
            continue
        c = _cittadinanza(riga)
        if c and not re.match(r"\s*\d", riga):
            citt = c
        f = _campi(riga)
        e0 = norm(f[0])
        if e0.startswith("eta/anno") or e0.startswith("eta / anno"):
            if somma is not None and trovato is None and citt_blocco == "tutte" and sesso_blocco in (None, "totale"):
                trovato = totale if totale and any(totale) else somma
            anni = [_num(x) for x in f[1:]]
            somma, totale, sesso = [0] * len(anni), None, None
            citt_blocco, sesso_blocco = citt, None
            continue
        if anni is None:
            continue
        if e0 in ("eta", "età") and len(f) > 1:
            sesso_blocco = _sesso(f[1]) or sesso_blocco
            if blocchi is not None:
                blocchi.append(f"{citt_blocco}/{f[1]}")
            continue
        valori = [_num(x) for x in f[1:1 + len(anni)]]
        if e0.startswith("total"):
            totale = valori
        elif f[0] and _num(f[0].split()[0]) is not None:  # 0, 1, ..., "100 e oltre"
            somma = [x + (y or 0) for x, y in zip(somma, valori + [0] * (len(anni) - len(valori)))]
    if somma is not None and trovato is None and citt_blocco == "tutte" and sesso_blocco in (None, "totale"):
        trovato = totale if totale and any(totale) else somma
    if anni is None:
        raise ValueError(f"riga 'Età/Anno' non trovata in {nome}")
    if trovato is None:
        raise ValueError(f"blocco 'Tutte le cittadinanze / Totale' non trovato in {nome}")
    return {cod: {x: v for x, v in zip(anni, trovato) if x and v is not None}}


def leggi_ric_territorio(testo: str, nome: str, blocchi: list | None = None) -> dict[str, dict[int, int]]:
    """Un file per provincia, a blocchi: '<cittadinanza> - Anno: AAAA - Provincia: ...',
    'Territorio/Età;;0;1;...', 'Codice comune;Comune;<sesso>', poi una riga per comune.
    Si tengono solo i blocchi 'Tutte le cittadinanze' + 'Totale'."""
    out: dict[str, dict[int, int]] = defaultdict(dict)
    anno, citt, sesso, ha_totale = None, "tutte", None, False
    for riga in testo.splitlines():
        if not riga.strip():
            continue
        m = re.search(r"Anno:\s*(\d{4})", riga)
        if m:
            anno = int(m.group(1))
            citt = _cittadinanza(riga) or "tutte"
            sesso = None
            continue
        f = _campi(riga)
        e0 = norm(f[0])
        if e0.startswith("territorio/eta"):
            ha_totale = norm(f[-1]).startswith("total")
            sesso = None
            continue
        if e0.startswith("codice comune") and len(f) > 2:
            sesso = _sesso(f[2])
            if blocchi is not None:
                blocchi.append(f"{anno}/{citt}/{f[2]}")
            continue
        if anno and citt == "tutte" and sesso in (None, "totale") and re.fullmatch(r"\d{6}", f[0]):
            valori = [v for v in (_num(x) for x in f[2:]) if v is not None]
            if valori:
                out[f[0]][anno] = valori[-1] if ha_totale else sum(valori)
    return out


def leggi_ric_zip(raw: bytes, nome_zip: str) -> dict[str, dict[int, int]]:
    pop: dict[str, dict[int, int]] = defaultdict(dict)
    singola = "singolaarea" in nome_zip.lower().replace("-", "").replace("_", "")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        nomi = [n for n in sorted(z.namelist()) if n.lower().endswith((".csv", ".txt"))]
        log(f"  {len(nomi)} file CSV nello zip")
        errori: list[str] = []
        for i, n in enumerate(nomi):
            testo = _decodifica(z.read(n))
            blocchi = [] if i == 0 else None
            if i == 0:
                log(f"  {n}:")
                for r in testo.splitlines()[:9]:
                    log(f"    | {r[:160]}")
            try:
                parte = leggi_ric_singola_area(testo, n, blocchi) if singola else leggi_ric_territorio(testo, n, blocchi)
            except ValueError as e:
                errori.append(str(e))
                continue
            if blocchi is not None:
                log(f"  blocchi nel primo file: {blocchi[:12]}{' ...' if len(blocchi) > 12 else ''}")
                log(f"  valori letti: {dict(list(parte.items())[:2])}")
            for cod, serie in parte.items():
                pop[cod].update(serie)
    if errori:
        log(f"  {len(errori)} file non letti, es.: {errori[:3]}")
        if len(errori) > 0.01 * max(len(nomi), 1):
            raise ValueError(f"troppi file non leggibili ({len(errori)} su {len(nomi)})")
    return pop


def carica_ricostruzione(anni_validi: set[int]) -> tuple[dict, str]:
    pop: dict[str, dict[int, int]] = defaultdict(dict)
    urls = trova_url_ricostruzione()
    for url in urls:
        log(f"RIC: scarico {url}")
        raw = scarica(url)
        if not raw:
            continue
        try:
            pop = leggi_ric_zip(raw, url.rsplit("/", 1)[-1])
        except Exception as e:  # formato inatteso: prova il link successivo
            log(f"  lettura fallita: {e}")
            pop = defaultdict(dict)
            continue
        tot = {a: sum(s.get(a, 0) for s in pop.values()) for a in sorted(anni_validi)}
        log("  totali Italia: " + ", ".join(f"{a}={t:,}" for a, t in tot.items()))
        if pop and all(50_000_000 < t < 65_000_000 for t in tot.values()):
            return pop, f"Ricostruzione intercensuaria ({url})"
        log("  totali non plausibili, scarto questa fonte")
        pop = defaultdict(dict)

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
                log("  prime righe:\n" + df.head(12).to_string(max_cols=12, max_colwidth=25))
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
        "nome": df[c_nom].fillna("").astype(object).map(str).str.strip(),
        "sigla": df[c_sig].fillna("").astype(object).map(str).str.strip() if c_sig else "",
        "regione": df[c_reg].fillna("").astype(object).map(str).str.strip() if c_reg else "",
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
        if isinstance(a, str) and isinstance(b, str) and re.fullmatch(r"\d{6}", a) and re.fullmatch(r"\d{6}", b):
            m[a].add(b)
    return m


def variazioni_note(vigenti: set[str]) -> tuple[dict[str, list[str]], list[tuple[int, str, str, str]]]:
    """Note leggibili sulle variazioni dal 2002 (report 129) e coppie (anno, comune, comune collegato, tipo)."""
    note: dict[str, list[str]] = defaultdict(list)
    eventi: list[tuple[int, str, str, str]] = []
    try:
        df = situas(129)
    except Exception as e:
        log(f"SITUAS 129 non disponibile, niente note: {e}")
        return note, eventi
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
        return note, eventi
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
        if b and b != a:
            eventi.append((int(anno), a, b, desc))
    return note, eventi


def stima_comuni_nuovi(serie, tipo, anni, vigenti, eventi, extra_note) -> None:
    """Comuni nati da parti di altri comuni (es. Misiliscemi da Trapani nel 2021).

    Prima della nascita il comune nuovo non ha dati e il comune che ha ceduto il territorio
    è ai confini vecchi. Si stima la quota ceduta nel primo anno in cui il comune nuovo esiste:
        quota_d = T_d / (pop_d(Y) + T_d),  T_d = pop_nuovo(Y) * peso_d
    dove peso_d è proporzionale al calo di popolazione del cedente d tra Y-1 e Y (uguale se nessun calo).
    Per gli anni precedenti: pop_nuovo(t) = somma_d pop_d(t) * quota_d e pop_d(t) diminuisce di altrettanto.
    I totali nazionali non cambiano."""
    for c in sorted(vigenti):
        presenti = [a for a in anni if a in serie[c]]
        if not presenti or presenti[0] == anni[0]:
            continue
        Y = presenti[0]
        buchi = [t for t in anni if t < Y and t not in serie[c]]
        cedenti = {o for (yr, a, b, _) in eventi if Y - 2 <= yr <= Y and c in (a, b)
                   for o in (a, b) if o != c and o in vigenti and (Y - 1) in serie[o] and Y in serie[o]}
        if not cedenti:
            log(f"  {c}: dati solo dal {Y}, nessun comune cedente trovato in SITUAS 129")
            continue
        cali = {d: max(0, serie[d][Y - 1] - serie[d][Y]) for d in cedenti}
        tot_cali = sum(cali.values())
        pesi = {d: (cali[d] / tot_cali if tot_cali else 1 / len(cedenti)) for d in cedenti}
        quote = {}
        for d in cedenti:
            T = serie[c][Y] * pesi[d]
            quote[d] = min(0.95, T / (serie[d][Y] + T)) if (serie[d][Y] + T) else 0
        for t in buchi:
            for d, q in quote.items():
                if t not in serie[d]:
                    continue
                parte = int(round(serie[d][t] * q))
                serie[c][t] = serie[c].get(t, 0) + parte
                serie[d][t] -= parte
                tipo[c][t] = tipo[d][t] = "P"
        if buchi:
            desc = ", ".join(f"{d} {q:.1%}" for d, q in sorted(quote.items()))
            log(f"  {c}: anni {buchi[0]}-{buchi[-1]} stimati come quota dei comuni cedenti ({desc})")
            extra_note[c].append(f"{buchi[0]}-{buchi[-1]}: stima, quota della popolazione dei comuni da cui è nato ({desc})")
            for d, q in quote.items():
                extra_note[d].append(f"{buchi[0]}-{buchi[-1]}: stima ai confini attuali, tolta la quota ceduta a {c} ({q:.1%})")


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

    # 3b. Comuni nati da parti di altri comuni
    note, eventi = variazioni_note(vigenti)
    extra_note: dict[str, list[str]] = defaultdict(list)
    log("Comuni nuovi da scorporo:")
    stima_comuni_nuovi(serie, tipo, anni, vigenti, eventi, extra_note)

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
    for cod in ("081021", "081025"):  # Trapani, Misiliscemi
        log(f"VERIFICA {cod}: " + ", ".join(f"{a}={serie.get(cod, {}).get(a)}" for a in (2002, 2019, 2020, 2021, 2022, ultimo)))

    # 4. Note
    for c, righe in extra_note.items():
        note[c].extend(righe)
    for c in vigenti:
        orig = sorted({o for a in anni for o in fonti[c].get(a, set())} - {c})
        if orig:
            note[c].insert(0, "Comprende i codici storici " + ", ".join(orig))

    # 5. Output
    vig = vig.set_index("codice")
    righe_csv, comuni_json, righe_api = [], [], []
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
        righe_api.append({"codice": c, "comune": r["nome"], "sigla": r["sigla"], "regione": r["regione"],
                          "valori": valori, "tipi": "".join(tipo[c][a] for a in anni), "note": nota})

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
    scrivi_api(righe_api, anni, meta)
    log(f"Scritti {len(comuni_json)} comuni, anni {anni[0]}-{anni[-1]}")


# --------------------------------------------------------------------------
# API statica (servita da GitHub Pages sotto /api/v1/)
# --------------------------------------------------------------------------
TIPI = {"R": "dato Istat", "S": "somma fusione", "P": "stima proporzionale", "-": "mancante"}


def scrivi_json(percorso: str, obj) -> None:
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def scrivi_api(righe: list[dict], anni: list[int], meta: dict, base: str = "api/v1") -> None:
    """righe: [{codice, comune, sigla, regione, valori, tipi, note}]"""
    if os.path.isdir(base):
        shutil.rmtree(base)  # niente file orfani di comuni soppressi
    indice = [{"codice": r["codice"], "comune": r["comune"], "sigla": r["sigla"], "regione": r["regione"]} for r in righe]
    scrivi_json(f"{base}/index.json", {"meta": meta, "comuni": indice})
    scrivi_json(f"{base}/meta.json", meta)

    for r in righe:
        scrivi_json(f"{base}/comuni/{r['codice']}.json", {
            "codice": r["codice"], "comune": r["comune"], "sigla": r["sigla"], "regione": r["regione"],
            "confini_al": meta["confini_al"],
            "serie": [{"anno": a, "popolazione": v, "tipo": TIPI[t]} for a, v, t in zip(anni, r["valori"], r["tipi"])],
            "note": r["note"],
        })

    for i, a in enumerate(anni):
        scrivi_json(f"{base}/anni/{a}.json", {
            "anno": a, "confini_al": meta["confini_al"], "totale_italia": meta["totali_italia"][str(a)],
            "comuni": {r["codice"]: r["valori"][i] for r in righe},
        })

    def aggrega(gruppo: list[dict]) -> list:
        return [sum(r["valori"][i] or 0 for r in gruppo) for i in range(len(anni))]

    per_prov = defaultdict(list)
    per_reg = defaultdict(list)
    for r in righe:
        per_prov[r["sigla"]].append(r)
        per_reg[r["regione"]].append(r)
    for sigla, gruppo in per_prov.items():
        if not sigla:
            continue
        scrivi_json(f"{base}/province/{sigla}.json", {
            "sigla": sigla, "regione": gruppo[0]["regione"], "anni": anni, "totale": aggrega(gruppo),
            "comuni": {r["codice"]: {"comune": r["comune"], "valori": r["valori"]} for r in gruppo},
        })
    regioni = []
    for nome, gruppo in per_reg.items():
        if not nome:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", norm(nome)).strip("-")
        regioni.append({"regione": nome, "id": slug})
        scrivi_json(f"{base}/regioni/{slug}.json", {
            "regione": nome, "anni": anni, "totale": aggrega(gruppo),
            "province": sorted({r["sigla"] for r in gruppo}),
        })
    scrivi_json(f"{base}/regioni/index.json", sorted(regioni, key=lambda x: x["id"]))
    log(f"API: {len(righe)} comuni, {len(anni)} anni, {len(per_prov)} province, {len(regioni)} regioni in {base}/")


if __name__ == "__main__":
    try:
        ricostruisci()
    except Exception as e:
        log(f"ERRORE: {e}")
        sys.exit(1)
