# popolazione-ricostruita

Serie storica della popolazione residente al 1° gennaio, **2002–2026**, per tutti i comuni italiani
**ai confini vigenti oggi**. Consultazione: https://cimbelli.github.io/popolazione-ricostruita/

## Fonti
| Anni | Fonte |
|---|---|
| 2002–2018 | Istat, Ricostruzione intercensuaria della popolazione (demo.istat.it, `i=RIC`), classificazione comuni 2019. Se il file non si trova, ripiego sui file RCS per cittadinanza, che per questi anni derivano dalla stessa ricostruzione |
| 2019–2026 | Istat, POSAS – popolazione residente al 1° gennaio (`demo.istat.it/data/posas/POSAS_AAAA_it_Comuni.zip`) |
| Raccordo | SITUAS report 99 (traslazione dei codici dal 1/1 di ogni anno a oggi), 61 (comuni vigenti), 129 (variazioni, per le note), via [`opensituas`](https://pypi.org/project/opensituas/) |

## Metodo
Per ogni anno il codice di ciascun comune viene portato al codice vigente oggi con il report SITUAS 99.
* **R** – comune invariato: dato Istat tal quale
* **S** – più comuni di allora confluiti in uno attuale (fusione/incorporazione): somma
* **P** – comune di allora diviso tra più comuni attuali e non più esistente: ripartizione in proporzione alla popolazione attuale dei comuni di destinazione

* **P** anche per i comuni **nati da una parte di un altro** (es. Misiliscemi, staccato da Trapani nel 2021):
  nel primo anno in cui il comune nuovo esiste si calcola la sua quota sul totale "cedente + nuovo"
  (con più cedenti, ripartita in base al calo di ciascuno); negli anni precedenti la popolazione del cedente
  ai confini vecchi viene divisa con quella quota. Cedente e comune nuovo sono marcati come stima, i totali non cambiano.

* **P** anche per le **cessioni di territorio tra comuni che esistono già entrambi** (dal 2019): la popolazione
  trasferita è la voce "Unità in più/meno dovute a variazioni territoriali" del bilancio demografico Istat (P2,
  `demo.istat.it/data/p2/P2_AAAA_it_Comuni.zip`). Quota = abitanti ceduti / popolazione del cedente al 1° gennaio
  dell'anno della cessione; negli anni precedenti quella quota passa dal cedente al ricevente.
  Per il 2002-2018 non serve: la ricostruzione Istat è già alla classificazione 2019.

Lo script si ferma con errore se i totali nazionali non sono plausibili o se il raccordo perde popolazione:
in quel caso l'Action fallisce e i dati pubblicati non vengono toccati.

## Aggiornamento
GitHub Action `update.yml`, il 1° di ogni mese o a mano (Actions → *Aggiorna Serie Storica* → *Run workflow*).
Se Istat cambia l'indirizzo del file comuni della ricostruzione, si può forzare con la variabile di repository `RIC_URL`.

## File
* `data/popolazione_2026.csv` – una riga per comune, una colonna per anno, più colonne di tracciabilità
* `data/popolazione_2026.json` – stessi dati in forma compatta per la pagina web

## API
File JSON statici, aggiornati insieme ai dati e serviti da GitHub Pages (CORS aperto, chiamabili da qualsiasi sito o script).
Base: `https://cimbelli.github.io/popolazione-ricostruita/api/v1/`

| Endpoint | Contenuto |
|---|---|
| `index.json` | elenco dei comuni vigenti (codice, nome, sigla, regione) + metadati |
| `meta.json` | fonti, data di aggiornamento, confini di riferimento, totali Italia per anno |
| `comuni/{codice}.json` | serie 2002-2026 di un comune, con il tipo di ogni valore e le note (es. `comuni/058091.json`) |
| `anni/{anno}.json` | tutti i comuni in un anno: `{codice: popolazione}` (es. `anni/2010.json`) |
| `province/{sigla}.json` | totale provinciale per anno e serie di ogni comune (es. `province/RM.json`) |
| `regioni/index.json`, `regioni/{id}.json` | totale regionale per anno (es. `regioni/lazio.json`) |

Esempi:
```bash
curl -s https://cimbelli.github.io/popolazione-ricostruita/api/v1/comuni/058091.json
```
```python
import pandas as pd
s = pd.read_json("https://cimbelli.github.io/popolazione-ricostruita/api/v1/comuni/058091.json")["serie"]
```
```r
jsonlite::fromJSON("https://cimbelli.github.io/popolazione-ricostruita/api/v1/anni/2010.json")$comuni
```
