---
title: A shared inbox that sorts itself
---
# A shared inbox that sorts itself

## The problem

A dental practice's front desk shares one inbox. Patients, suppliers and colleagues all write to it. Someone reads
every message to decide who should handle it, what to do with it and how soon. A message about swelling after
treatment that waits until morning is a real problem, and so is a supplier's bill that sits unpaid.

## How Jeff helps

[Message triage](/catalogue/jeff.support.message-triage) reads one message against the practice's own lists: its
kinds of message, its routing table (who handles what) and its routing rules. It answers three questions, each with
its own function:

- `route_to`: which entry of your routing table handles it. Your table's keys are the answers.
- `action_needed`: reply, forward, do a task, read for information only, or ignore as spam.
- `urgency`: now, today, this week, or no deadline.

Your code then forwards, files or flags the message. Nothing about the practice is trained into the model: change the
routing table and the answers follow it.

## The solution

Each step below is a live call; the answer under it is the one the API returned. Choose your language and whether to
run it on your machine (free) or through the API.

### A patient with swelling after a filling

The message asks two things: whether the swelling is normal, and to move an appointment. The practice's rule says
anything about pain, swelling or bleeding after treatment goes to the duty dentist, whatever else it asks.

<!-- example: worked-inbox-swelling-route -->

`clinical`: the rule wins over the appointment question, so the message goes to the duty dentist.

<!-- example: worked-inbox-swelling-action -->

`forward`: the front desk passes it on rather than answering it.

<!-- example: worked-inbox-swelling-urgency -->

`today`: the patient asked to be called back tonight.

### A supplier's bill

<!-- example: worked-inbox-invoice-route -->

`billing`: supplier bills belong to the practice's accounts.

<!-- example: worked-inbox-invoice-action -->

`forward`: the front desk passes the bill on to be paid.

<!-- example: worked-inbox-invoice-urgency -->

`this_week`: payment is due on Friday 6 November.

## What you get

- Three answers per message from three calls, each with a confidence your code can check before it acts. All six
  answers above were above their functions' thresholds.
- The sorting follows the practice's own table and rules, passed as context, so a new team or rule needs no new
  model.
- How [Message triage](/catalogue/jeff.support.message-triage) measures: 98.0% on CLINC150, a public intent
  benchmark (1,000 rows; it asks only the intent question), and 96.7% on its in-house test (2,000 rows, from the
  same generator as the training data). When confident, `route_to` is right 96.9% of the time, `action_needed` 95.2%
  and `urgency` 98.25%. The in-house rows were kept only where a blind LLM check agreed with the label, so those
  scores are probably inflated.
