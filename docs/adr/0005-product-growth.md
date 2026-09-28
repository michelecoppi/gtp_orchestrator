# ADR 0005 — Prodotto e growth: metriche del gioco, fattibilità calcolata, brief in bozza

**Stato:** accettata · **Data:** 29/09/2026

## Contesto
M4 della specifica chiede un funnel verificato e brief per Promo in bozza. I criteri: metriche riconciliabili,
coorti mature e gestione dei dati mancanti.

Il gioco ha già definizioni, finestre, soglie minime (30 utenti) e una procedura di review settimanale
(`docs/product-analytics.md` §12 e §15b). La baseline del 27/09 registra anche un problema di qualità dei dati:
`bot_started` è assente mentre gli eventi Daily arrivano.

## Decisione
1. **Stesse definizioni del gioco.** Le query HogQL riprendono le insight salvate: filtro
   `environment=production`, `is_new_user = 'true'` come stringa, primo evento di sempre, coorti mature. A
   queste si aggiunge la North Star della specifica: nuovi ingressi che completano una Daily nella Mini App
   entro 7 giorni.
2. **Ogni tasso porta con sé conteggi e stato.** Gli stati sono `ok`, `sotto_soglia` (solo descrittivo),
   `non_disponibile` (nessun denominatore, mai 0%) e `immatura`.
3. **Collector con cache.** PostHog si interroga al massimo ogni 20 ore; nei giri intermedi i fatti vengono dal
   cursore, con la data di raccolta. Se una query fallisce non si aggiorna la cache.
4. **La fattibilità la decide il codice.** Il modello propone metrica e miglioramento minimo; il codice calcola
   la numerosità dal tasso di partenza reale (solo sopra soglia) e dal volume settimanale. Oltre 8 settimane la
   proposta diventa qualitativa. I numeri nel testo che non compaiono nei dati vengono segnalati.
5. **Una proposta a settimana, idempotente.** È salvata in `proposals/{settimana ISO}`.
6. **Brief per Promo in bozza.** Contiene solo fatti verificati (`config/promo_facts.toml`, ognuno con la sua
   fonte), valori da elenchi chiusi (lingua, formato, canale) e `campaign_id` generato dal codice. Il supporto
   nel gioco e in Promo è proposto come issue (`docs/proposals/`), non implementato da qui: ogni repository
   segue il proprio processo.
7. **Qualità dei dati come finding.** Un evento chiave assente è un finding (`analytics_data_quality`), non un
   crollo del funnel.

## Conseguenze
- Con i volumi attuali (pochi utenti al giorno) quasi tutte le proposte saranno qualitative o riguarderanno i
  dati. È il risultato corretto: nessun "vincitore" su numeri piccoli.
- Serve una Personal API Key PostHog in sola lettura (`SUP_POSTHOG_PERSONAL_API_KEY`). Senza chiave la sorgente
  risulta "non configurata".
- Le query sono verificate con fixture; la prima esecuzione reale va controllata sul riepilogo del run.
