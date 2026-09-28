# ADR 0001 — Stato del supervisore su Firestore, in un progetto GCP dedicato

**Stato:** accettata · **Data:** 28/09/2026

## Contesto
I job girano su GitHub Actions, con disco effimero. Budget, lock, cursori, approvazioni e contabilità
richiedono un archivio transazionale esterno ai runner (specifica, sez. 7.2). I due repository usano già
Firestore e Workload Identity Federation.

## Decisione
- In produzione lo stato sta su **Firestore in un progetto GCP dedicato** al supervisore, separato dal database
  utenti del gioco e dalle sue credenziali di scrittura.
- Sul progetto del gioco il service account del supervisore ha solo `roles/datastore.viewer`, per leggere
  `promo_posts`.
- In locale e nei test si usano SQLite e la memoria, con lo stesso contratto (`tests/test_store_contract.py`,
  eseguito anche sull'emulatore Firestore in CI).

## Garanzie
- Eventi: id del documento = `dedupe_key`, creazione con `create()`. Un duplicato viene rifiutato.
- Cursori: scritti dopo gli eventi del loro batch. Un crash nel mezzo porta a rileggere, mai a perdere.
- Lock: documento `locks/observe` con lease, acquisito in transazione. Si aggiunge a `concurrency` di Actions.
- Notifiche: chiave prenotata in transazione prima dell'invio.
- Consegna almeno una volta con deduplicazione. Non si promette l'exactly-once.

## Conseguenze
- Serve un nuovo progetto GCP con Firestore attivo (costo atteso trascurabile a questi volumi, da verificare).
- Le query per intervallo di tempo usano campi singoli (`received_at`, `created_at`, `started_at`,
  `collected_at`) e non richiedono indici composti.
