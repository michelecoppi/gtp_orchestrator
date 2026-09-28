# Proposta di issue — Promo Studio: ricezione dei brief del supervisore

**Repository:** `michelecoppi/promo_studio` · **Tipo:** feature · **Stato:** implementata e chiusa il 29/09/2026 ([promo_studio#1](https://github.com/michelecoppi/promo_studio/issues/1), PR #2, commit `c750cef`); campi letti da `brief-import`: campaign_id, language, format, channel, cta, angle, facts, day

---

**Titolo:** Brief strutturati dal supervisore: `brief_id`/`campaign_id` sulle bozze

## Problema
Oggi le bozze nascono solo dalla rotazione per giorno della settimana (`plan.ROTATION`) e il link di
tracciamento contiene soltanto il canale (`?start=src_<canale>`). Il supervisore (gtp_orchestrator, M4)
prepara ogni settimana, in bozza, un brief con: pubblico, lingua, formato, canale, CTA, angolo e soli fatti
verificati. Promo però non lo può ricevere, e le pubblicazioni non si possono legare ai giocatori attivati.

## Proposta
- Nuovo comando `python -m promo brief-import <file.json>` che legge un brief del supervisore:
  - campi: `campaign_id`, `language`, `format` (uno di `FORMATS`), `channel` (uno di `CHANNELS`), `cta`,
    `angle`, `facts[]`;
  - crea bozze con `plan.make_post` usando formato e lingua del brief;
  - il picker anti-spoiler resta invariato.
- Nuovi campi del post: `brief_id` (= `campaign_id`) e `tracking_link` =
  `https://t.me/<bot>?start=src_<canale>-<campaign_id>`. Serve la issue sul gioco per `campaign_id`; finché
  non è unita il link cade su `src_<canale>`.
- Le caption restano quelle dei template versionati. Il brief suggerisce solo formato, lingua e canale: nessun
  testo generato entra nella caption senza approvazione.
- La macchina a stati non cambia: l'approvazione resta umana, dal bot o dalla dashboard.

## Criteri di completamento
- [ ] `brief-import` crea bozze idempotenti (stesso `campaign_id` e giorno → nessun duplicato).
- [ ] Valori fuori elenco (formato, canale, lingua) rifiutati con un messaggio chiaro.
- [ ] `tracking_link` con `campaign_id` solo se il gioco lo supporta (impostazione), altrimenti come oggi.
- [ ] `docs/promo-studio.md` aggiornato; test senza rete.
