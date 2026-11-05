---
title: Support
---
# Support

The support adapters sort what arrives in an inbox or a support queue: what type of message it is, what it asks for, whether
a team must act, and who should handle it. Most of them take your own categories, teams and rules as context or as
labels, so the same function serves an office manager's inbox, a council team's mailbox and an online shop's help
desk. The page ends with the trading desk adapter, which applies a desk's written rules to the market in the same
way.

Each example below uses an input written for this page and shows the answer returned by the live API. To run one yourself, set `JEFF_API_KEY` as in the [Quick start](/docs/guides/quickstart).

## Shared inbox triage

[Shared inbox triage](/catalogue/jeff.support.message-triage) triages a message using the inbox's own message
types, routing table and rules: what it is, what action it needs, how urgent it is and who should handle it. Each of
those is a separate function; this example asks what an architecture practice's office manager should do with
a supplier's invoice:

<!-- example: oss-message-triage -->

`forward` means a team in the routing table should deal with it and is not yet on the message: here the finance
team, which handles supplier invoices.

## Team action and escalation check

[Team action and escalation check](/catalogue/jeff.support.requires-action) reads a message against a team's policy and
decides whether the team must act on it, and whether it must be escalated: yes, no or unclear. Here a council's
highways team receives a report of a pothole:

<!-- example: oss-requires-action -->

`yes` means the team is responsible for acting on the message: potholes are on its list.

## Support ticket triage

[Support ticket triage](/catalogue/jeff.support.triage) routes a support ticket or email to a team and scores its
urgency, sentiment and need for a person. This example asks for the team, from the teams you pass as labels:

<!-- example: oss-triage -->

`orders` is the key of the team to send it to: exchanging an incorrect item is an orders matter, not a payments issue.

## Customer request classification

[Customer request classification](/catalogue/jeff.support.support-intents) names what a customer or assistant user is
asking for, from a fixed list of requests such as `cancel_order` or `track_refund`. Here a shop's chat receives a
message about an order placed yesterday:

<!-- example: oss-support-intents -->

`cancel_order` is the request to act on: the boots have not shipped, so this is a cancellation, not a return.

## Trading desk decisions

[Trading desk decisions](/catalogue/jeff.finance.trading-desk) applies a trading desk's written rules to the live
order book and decides whether to buy, sell or hold, the size and venue, how to handle open orders, and when to go risk-off.
This example asks only whether a risk-off trigger has fired:

<!-- example: oss-trading-desk -->

`yes` means the desk should go risk-off: the day's loss on this stock (26,400) is beyond the 25,000 limit the rules
set, whatever the price signal says.
