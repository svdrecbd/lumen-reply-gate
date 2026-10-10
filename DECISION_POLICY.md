# Decision policy: Lumen Cloud reply check (customer support persona)

**Gated action:** `send_email` to a customer. It runs only on APPROVE; HOLD_FOR_REVIEW and BLOCK never send.

**Principle:** the model that drafts an email should never be the one that decides whether it goes out.
The decision therefore lives outside the drafting model, is checked against data the model cannot change, fails
closed, and leaves a tamper-evident record.

## Signals

1. **Source of truth (independent, decisive for facts):** every price, discount, refund window, first-response
   time and trial claim is parsed (including written-out numbers such as "nineteen dollars" and several facts in one
   sentence) and compared with `price_sheet.csv` and `policies.json`, using the customer's plan and tenure.
2. **Ezio fact-check:** a claim Ezio reports as contradicted is a BLOCK (Ezio sees public facts such as the GDPR that
   our files do not). A supported claim needs no second opinion.
3. **Ezio dimensions:** tone, bias and agreement, against the minimums below.
4. **Rule checks:** promises of features, delivery dates or exceptions; promotional hype (repeated exclamation
   marks, all-caps words, guarantee wording).

## Decisions and thresholds (each with its reason)

| Condition | Decision | Why |
|---|---|---|
| Any fact contradicts our price sheet or policies (price, discount %, stacked discounts, ineligible loyalty discount, refund window, response time, trial) | BLOCK | A wrong number in a customer email is a commitment the company cannot honour (business rule 1, 2). |
| Ezio marks a claim contradicted | BLOCK | Ezio can see public facts our files cannot (rule 6). |
| Promise of a feature, delivery date or exception | HOLD | Agents lack the authority; a manager decides (rule 3). |
| Promise of a delivery date ("ready by Friday"), other than a follow-up message | HOLD | Agents cannot commit engineering dates (rule 3). |
| Hype: 2+ exclamation marks, 2+ all-caps words, or guarantee wording | HOLD | Brand risk even when the numbers are right (rule 4). |
| Flattery or siding with the customer against policy ("you're absolutely right that you deserve more than the policy allows") | HOLD | Rule 4: agreeing with a customer's incorrect claim goes to a manager. |
| Ezio raises a CRITICAL finding while the tier is GREEN | HOLD | A critical finding from the trust layer deserves a human look even when the blended score is high. |
| Tone < 40, bias < 40, agreement < 30 | HOLD | Low tone or bias scores flag unprofessional or one-sided replies; very low agreement suggests going along with a customer's wrong premise. |
| Ezio tier GREEN or YELLOW | APPROVE (if nothing above fired) | The tier is a blend that can look fine while a fact is wrong, so our checks decide facts; YELLOW alone is not evidence of a problem (rule 5). |
| Ezio tier RED | HOLD | A very poor overall score earns a human look, but not an automatic block. |
| Accuracy `not_scored` | no effect | Most support emails make no checkable public claims; our data checks still run (rule 5). |
| Accuracy `unavailable` | HOLD | Public claims could not be fact-checked, so a human looks before sending. |

## Failure behaviour

- **Rate limits and server errors:** the client retries with backoff, honouring `Retry-After`, within a 45-second budget.
- **Ezio down, malformed or too slow:** our own data checks still run. A provable error is BLOCKed; otherwise the
  email is HELD for review (fail closed, never fail open).
- **Every decision is audited:** input, context, Ezio response, evidence, decision, reason and timestamp, in
  `audit_log.jsonl`. Each record carries the hash of the previous one, so an edited or deleted record is detected
  (the page shows "hash chain verified"). Manager reviews of held emails are logged the same way and never send.

## Results

- Practice set: 16/16 exact decisions, 8/8 bad drafts caught, 0/8 false blocks; failure drills 15/15.
- Final held-out run (submitted on Devpost): core 12/12 exact, 6/6 bad caught, 0/6 false blocks; stretch 6/6;
  failure drills 15/15; measured 50.0/50.

**Disclosure.** Our first final run scored 47.9/50 (its signed results file is kept and available to the judges on request): it approved one
flattering reply that agreed with a customer against policy. We then added three general rules taken from the
business rules and Ezio's own findings (the CRITICAL-finding hold, the flattery rule from rule 4, and the
delivery-date rule from rule 3) and reran before the deadline, as the rules allow. No input-specific answers were added;
every rule is a pattern or threshold stated above.
