---
title: Safety and language
---
# Safety and language

The safety and language adapters identify the nature of a message: an attempt to take control
of an AI model, spam or a phishing scam, or the emotion behind it. They are fast enough to run on every message
before anything else does. The compliance section that follows applies an institution's own rules to customers and
alerts: sanctions screening and security alert triage.

Each example below uses an input written for this page and shows the answer returned by the live API. To run one yourself, set
`JEFF_API_KEY` as in the [Quick start](/docs/guides/quickstart).

## Prompt injection detection

[Prompt injection detection](/catalogue/jeff.safety.guard) checks a user message or outside content for prompt
injection, jailbreak and data-leak attempts, and names the kind. The [Quick start](/docs/guides/quickstart) shows
its first function, which detects an attempt; this example identifies the type of attempt in an email that an
inbox assistant is about to summarise:

<!-- example: oss-guard-kind -->

`exfiltration` means the text tries to get the assistant to send the user's data elsewhere: do not let the
assistant act on it, and summarise the email without following it.

## Spam and phishing

[Spam and phishing detection](/catalogue/jeff.safety.spam) says whether a text message or email is spam or
phishing, and whether it is legitimate, spam or a phishing scam. Here is a text message about a parcel:

<!-- example: oss-spam -->

`phishing` means the message is a scam after money or details, not just unwanted advertising: block the link or
warn the reader.

## Emotion detection in short text

[Emotion detection in short text](/catalogue/jeff.language.emotion) picks the strongest of 27 emotions, or neutral, in a
short comment or message. Here is a forum comment:

<!-- example: oss-emotion -->

`joy` is right, but its confidence is only 0.414. With 28 possible answers, and close neighbours such as pride,
relief and excitement, the probability is spread over several of them, and low confidence means the function is
uncertain: across many answers assigned a confidence of about 0.4, more are wrong than right. For a single comment, check `abstained`
(it is `true` when the confidence is below the threshold set during calibration) and treat the emotion in
uncertain cases as unclear, or read `probabilities` for the next most likely answers. Across many comments, the counts are
still useful. See [Calibrated confidence](/docs/guides/how-it-works#section-calibrated-confidence) and [Escalation](/docs/guides/escalation).

## Compliance

The compliance adapters apply the rules an institution has written down. They sort cases so that analysts start with
the ones that matter; they do not replace the analyst's decision.

### Sanctions and watch-list screening

[Sanctions and watchlist screening](/catalogue/jeff.compliance.sanctions) compares a customer or payment party with
one sanctions or watch-list entry and says match, possible match (check it) or no match. Here is a customer whose name
is close to a listed person's:

<!-- example: oss-sanctions -->

`no_match` means the customer is not the listed person: the name is close, but the date of birth and nationality
differ. A `possible_match` would go to an analyst.

### Security alert triage

[Security alert triage](/catalogue/jeff.security.soc) applies a security team's playbook to a new alert and decides
whether to ignore, investigate, isolate, block or escalate. Here is an alert from a laptop's endpoint protection:

<!-- example: oss-soc -->

`isolate` is right (the playbook's first rule: files renamed quickly on a laptop are ransomware behaviour), but its
confidence is 0.795, lower than a clear-cut rule match deserves. A confidence of 0.8 means about one answer in five
at that level is wrong, so automated network isolation at this confidence level requires further review. Check `abstained`
(it is `true` when the confidence is below the threshold set during calibration), and send uncertain
alerts to an analyst, or let Jeff pass them on with an `on_low_confidence` policy. See
[Calibrated confidence](/docs/guides/how-it-works#section-calibrated-confidence) and [Escalation](/docs/guides/escalation).
