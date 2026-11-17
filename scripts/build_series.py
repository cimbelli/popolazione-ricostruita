import json
import pandas as pd
import numpy as np
import requests
from io import StringIO

ANNI = [str(y) for y in range(2002, 2027)]

def carica_dati_situas():
    with open("data/raw/comuni_attuali.json", encoding="utf-8") as f:
        comuni = pd.DataFrame(json.load(f))
    with open("data/raw/variazioni_2019_oggi.json", encoding="utf-8") as f:
        variazioni = pd.DataFrame(json.load(f))
    return comuni, variazioni

def scarica_popolazione_istat():
    """
    Scarica via API SDMX Istat (formato CSV):
    1) Serie ricostruita 2002-2019 (ai confini 2019)
    2) Bilancio demografico 2019-2026 (con colonna variazioni territoriali)
    """
    headers = {"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}
    
    # Endpoint SDMX Istat per popolazione residente comunale
    url_ric = "https://istat.it"
    url_bil = "https://istat.it"
    
    r_ric = requests.get(url_ric, headers=headers, timeout=120)
    r_bil = requests.get(url_bil, headers=headers, timeout=120)
    
    df_ric = pd.read_csv(StringIO(r_ric.text))
    df_bil = pd.read_csv(StringIO(r_bil.text))
    return df_ric, df_bil

def ricostruisci():
    comuni_2026, variazioni = carica_dati_situas()
    # Nota: qui va mappato il tracciato colonne effettivo restituito dal tuo endpoint
    # Inizializziamo la struttura finale indicizzata sul codice Istat vigente (PRO_COM_T)
    
    risultati = []
    
    # Esempio di logica applicata per ciascun comune vigente nel 2026:
    # 1. Se il comune non ha variazioni post-2019 -> tutti gli anni hanno flag "R" (Reale)
    # 2. Se deriva da FUSIONE nell'anno T -> somma i comuni soppressi per anni < T, flag "S"
    # 3. Se ha subito SCORPORO/CESSIONE nell'anno T con delta_P abitanti spostati:
    #    alpha = delta_P / pop_cedente[T-1]
    #    Per ogni anno t < T:
    #      quota = int(round(pop_cedente[t] * alpha))  # Arrotondamento all'unità
    #      pop_cedente_ric[t] = pop_cedente[t] - quota # Somma zero garantita
    #      pop_ricevente_ric[t] = pop_ricevente[t] + quota
    #      flag[t] = "P" (Proporzione stimata)

    # Salvataggio CSV con colonna di tracciamento anni stimati
    df_out = pd.DataFrame(risultati)
    df_out.to_csv("data/popolazione_2026.csv", index=False, encoding="utf-8")

    # Salvataggio JSON leggero per la Web App
    with open("data/popolazione_2026.json", "w", encoding="utf-8") as f:
        json.dump(risultati, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    ricostruisci()
