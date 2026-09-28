# ADR 0003 — Budget con prenotazione atomica e gateway AI unico

**Stato:** accettata · **Data:** 28/09/2026

## Contesto
La specifica (sez. 5 e 9) chiede un tetto di spesa reale, 15 USD al mese e 1 USD al giorno. Un file YAML
o TOML da solo non basta. Servono anche:
- un'interfaccia LLM sostituibile;
- un solo responsabile dei tentativi;
- nessun fallback che faccia spendere senza controllo.

## Decisione
- Tutte le chiamate a pagamento passano da `llm/gateway.py::LLMGateway`. Nessun modulo chiama direttamente un
  client o LiteLLM.
- I controlli avvengono prima dell'invio, in questo ordine:
  1. policy `call_paid_llm = budget` con budget approvato (`config/budget.toml: approved = true`);
  2. `SUP_AI_ENABLED`;
  3. modello abilitato e con `access_verified = true`;
  4. prezzo valido per la data di Roma;
  5. input entro `max_input_tokens`, così i prezzi OpenAI restano nella fascia short context;
  6. limite di chiamate per task;
  7. nessuna chiamata precedente del task con esito incerto.
- `core/budget.py::BudgetLedger` prenota la stima pessimista in una transazione sullo stato, sui documenti
  `budget/{mese}` e `usage/{call_id}`. La stima conta l'input sovrastimato e l'output massimo, più il 10%. La
  condizione è `speso + prenotato + nuovo <= tetto`, sia giornaliero sia mensile. Gli importi sono interi in
  micro-dollari.
- Esiti:
  - **riuscita**: costo reale dai token × il prezzo del catalogo; se l'usage manca si addebita l'intera
    prenotazione;
  - **rifiutata** (4xx, 429): prenotazione rilasciata;
  - **incerta** (timeout, rete, 5xx, crash): prenotazione mantenuta. Il task resta bloccato finché Michele non
    riconcilia con `supervisor budget --reconcile`.
- Nessun retry nell'adapter LiteLLM (`num_retries=0`, `max_retries=0`) e nessun fallback nel routing.
- Il catalogo (`config/models.toml`) è versionato e si cambia solo a mano. `access_verified` passa a `true` solo
  dopo `supervisor llm smoke <modello>` e un controllo umano.
- Stato non disponibile o prezzo sconosciuto: nessuna chiamata. Report e regole deterministiche continuano a
  funzionare.

## Conseguenze
- Il tetto giornaliero può bloccare un singolo lavoro costoso, per esempio un'escalation Opus da 1,20 USD. Il
  sistema lo segnala e rinvia, senza frammentare la chiamata. Per farlo passare serve un'eccezione esplicita di
  Michele (in futuro).
- Chiamate fatte fuori dal gateway con le stesse chiavi non sono contate: le chiavi devono essere dedicate al
  supervisore e, dove possibile, vanno impostati limiti di spesa anche sul provider.
- In CI le prenotazioni concorrenti sono provate su SQLite e sull'emulatore Firestore
  (`tests/test_budget.py`).

## Aggiornamento 28/09/2026 — provider tramite OpenRouter
Michele non ha account diretti con OpenAI e Anthropic. Il routing punta quindi ai modelli `...@openrouter`: un
solo account, una sola chiave (`OPENROUTER_API_KEY`) e credito prepagato. Gli id e il listino sono stati
verificati sull'API pubblica di OpenRouter e coincidono con i prezzi dei provider.

I prezzi del catalogo includono la commissione del 5,5% sull'acquisto di credito, così la prenotazione resta
prudente. Le voci dirette restano nel catalogo: per tornare alle API dei provider basta cambiare
`config/routing.toml`, senza toccare il codice. Autore (OpenAI) e reviewer (Anthropic) restano modelli di
provider diversi. Sulla chiave OpenRouter va impostato anche un limite di spesa: vale come secondo tetto,
esterno al supervisore.
