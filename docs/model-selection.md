# Scelta dei modelli — 29/09/2026

Decisione presa sulle evaluation disponibili (specifica, sez. 14). Si aggiorna `config/routing.toml` solo a
mano e solo dopo una nuova evaluation. Prezzi del catalogo via OpenRouter, commissione del 5,5% inclusa.

| Funzione | Modello | Alternativa più vicina |
|---|---|---|
| `triage` | GPT-6 Luna | GPT-6 Sol (+9 punti sul ruolo, costo ~20×) |
| `engineer_plan`, `engineer_patch` | GPT-6 Sol | Claude Sonnet 5 (pari risultati, costo ~2×) |
| `engineer_review` | Claude Sonnet 5 | Gemini 3.8 Flash (provider diverso dall'autore, più economico, meno evidenze) |
| `growth_weekly` | GPT-6 Sol | Claude Opus 5.5 (nessun vantaggio misurato, costo ~2×) |

## Evidenze

**Triage.** 12 casi, 2 ripetizioni, run Evaluate 36482408796, 36485175559 e 36485982039. JSON valido al 100%
per tutti i modelli dopo le correzioni del parser.

| Modello | Priorità | Ruolo | Serve Michele | Costo per chiamata |
|---|---|---|---|---|
| GPT-6 Luna | 83% | 83% | 79% | $0.00015 |
| Gemini 3.8 Flash | 83% | 92% | 54% | $0.0008 |
| GPT-6 Sol | 83% | 92% | 62% | $0.0031 |
| Claude Sonnet 5 | 83% | 92% | 33% | $0.0040 |
| Claude Opus 5.5 | 83% | 88% | 38% | $0.0078 |

Luna ha la stessa accuratezza di priorità degli altri, è la migliore su "serve Michele" (i modelli Anthropic
chiedono quasi sempre un intervento umano) e costa 5–50 volte meno.

**Engineering.** 10 casi storici del gioco, test nascosti della fix reale.
- Primo giro (run 36485675692, harness con difetti di formato): Gemini 2/10, Sol 1/10, Sonnet 0/10 (Sonnet non
  superava la pianificazione).
- Secondo giro (run 36490137732, interrotto dopo 7 casi): 1/7 risolti per tutti e tre, 3 patch prodotte
  ciascuno. Costo per caso: Gemini $0.06, Sol $0.15, Sonnet $0.35.
- Sol non ha rotto test già verdi in nessun giro; Gemini ne ha rotto uno nel primo.

L'evidenza engineering è debole: i modelli non si distinguono ancora. Molti fallimenti dipendevano dall'harness
(formato, sostituzioni ambigue, casi fuori scopo) e sono stati corretti dopo il secondo giro (commit `0a6f4a7`).

## Da rifare (ottobre 2026, nuovo budget evaluation)
- Evaluation engineering completa, con le correzioni, su Sol, Sonnet 5, Gemini 3.8 Flash e **Opus 5.5**, con
  almeno 2 ripetizioni sui 5 casi nello scopo e altri casi storici nello scopo, se disponibili.
- Rivedere le etichette del triage con Michele (backup fallito e pubblicazione fallita tre volte: tutti i modelli
  dicono "media", le etichette dicono "alta").
