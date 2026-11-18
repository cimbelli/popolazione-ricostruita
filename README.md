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

Le cessioni parziali di territorio tra comuni che continuano a esistere non vengono riproporzionate
(il comune cedente tiene tutta la popolazione negli anni precedenti); compaiono nelle note.

Lo script si ferma con errore se i totali nazionali non sono plausibili o se il raccordo perde popolazione:
in quel caso l'Action fallisce e i dati pubblicati non vengono toccati.

## Aggiornamento
GitHub Action `update.yml`, il 1° di ogni mese o a mano (Actions → *Aggiorna Serie Storica* → *Run workflow*).
Se Istat cambia l'indirizzo del file comuni della ricostruzione, si può forzare con la variabile di repository `RIC_URL`.

## File
* `data/popolazione_2026.csv` – una riga per comune, una colonna per anno, più colonne di tracciabilità
* `data/popolazione_2026.json` – stessi dati in forma compatta per la pagina web
