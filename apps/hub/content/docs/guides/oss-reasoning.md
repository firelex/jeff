---
title: Reasoning
---
# Reasoning

The reasoning adapters compare one thing with another and give a short verdict: does this evidence support a claim,
do two records disagree, do two descriptions mean the same thing, does a document meet a requirement, is a pack of
documents complete? They are the checks a careful reviewer makes before acting, written out so your code can apply
them to every case. Each one answers with one of a few verdicts, one of which is always "unclear", so the
function can flag a case it cannot settle instead of forcing a yes or no answer. The page ends with a different kind of
adapter: one that answers multiple-choice general-knowledge questions.

Each example below uses an input written for this page and shows the answer returned by the live API. To run one yourself, set `JEFF_API_KEY` as in the [Quick start](/docs/guides/quickstart).

## Claim support check

[Claim support check](/catalogue/jeff.reasoning.entails) checks whether the evidence given (documents,
messages or records) supports a claim: supported, unsupported or unclear. Here an accounts payable team checks a
supplier's claim against the purchase order and the signed delivery note before paying:

<!-- example: oss-entails -->

The answer is `supported`, which is right (all 120 boards arrived on 26 February, before the 28 February deadline),
but its confidence is only 0.615. Low confidence means the function is uncertain: across many answers assigned a confidence of about
0.6, roughly four in ten are wrong. Do not act on such an answer alone. Check `abstained` (it is `true` when the
confidence is below the threshold set during calibration), and send uncertain cases to a person or a
larger model, or let Jeff do that with an `on_low_confidence` policy. See [Calibrated confidence](/docs/guides/how-it-works#section-calibrated-confidence) and
[Escalation](/docs/guides/escalation).

## Contradiction check

[Contradiction check](/catalogue/jeff.reasoning.contradicts) checks whether two statements or records conflict: yes,
no, or unclear (for example a date that can be read two ways). Here a clinic's record says the patient has no known
allergies, and a letter from the patient's doctor says otherwise:

<!-- example: oss-contradicts -->

`yes` means the two records conflict, so the record needs correcting before the appointment.

## Same meaning check

[Same meaning check](/catalogue/jeff.reasoning.equivalent) checks whether two items mean the same thing within a
stated scope, allowing for differences in wording, units and formats: yes, no or unclear. Here an online bookshop compares two
suppliers' listings before merging them:

<!-- example: oss-equivalent -->

`yes` means every fact the scope names (title, author, format, pages and year) agrees, so the two listings are the
same edition and can be merged.

## Requirement check

[Requirement check](/catalogue/jeff.reasoning.satisfies) checks whether an item (a document, message or record) meets
one stated requirement: pass, fail or unclear. Here a housing association's lettings officer needs proof of income
from the last three months:

<!-- example: oss-satisfies -->

`fail` means the payslip does not meet the requirement: it is dated May, not August, September or October.

The requirement names the months it accepts. Relative date windows, such as "dated within the three months before
the application", are a known weak spot of this version: the function does not reliably count months back from a
date. Calculate the window in your own code and write the requirement with the dates or months it allows, as here.

## Document completeness check

[Document completeness check](/catalogue/jeff.reasoning.is-complete) checks a pack of documents against a checklist:
complete, incomplete (something missing, expired, unsigned or about someone else) or unclear. Here a small business
lender checks a loan application pack before credit review:

<!-- example: oss-is-complete -->

`incomplete` means at least one checklist item is not met: the bakery has two directors, but the pack contains
photo identification for only one (the annual accounts are not needed, as the bakery has traded for less than two
years).

## Multiple-choice general knowledge

[Multiple-choice general knowledge](/catalogue/jeff.knowledge.knowledge-qa) answers multiple-choice science, commonsense
and general-knowledge questions by picking the right option. You pass the options as the labels. Here a pharmacy
assistant is asked why a patient on warfarin needs more blood tests during a course of antibiotics:

<!-- example: api-knowledge-qa -->

`B` is the right option: some antibiotics strengthen warfarin's effect and raise the risk of bleeding. For a
question that depends on your own documents, pass the facts in or use a retrieval adapter instead.
