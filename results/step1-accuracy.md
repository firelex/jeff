# Step 1 accuracy check

A fixed-seed sample of 400 test rows per adapter. Engine mode sends each row as written (the published measurement); template mode rebuilds each row as a user's call through the manifest and skips rows of shapes the manifest does not describe. Intervals are 95% Wilson intervals.

| Adapter | Published | Engine acc [interval] | Template acc [interval] | Skipped |
|---|---|---|---|---|
| jeff.compliance.aml | 95.0% | 96.8% [94.5%, 98.1%] | 96.8% [94.5%, 98.2%] | 27 |
| jeff.compliance.sanctions | 100.0% | 99.8% [98.6%, 100.0%] | 99.8% [98.6%, 100.0%] | 0 |
| jeff.finance.trading-desk | 98.1% | 98.0% [96.1%, 99.0%] | 98.0% [96.1%, 99.0%] | 0 |
| jeff.language.emotion | 60.5% | 58.8% [53.9%, 63.5%] | 61.5% [56.6%, 66.1%] | 0 |
| jeff.legal.legal-clauses | 83.6% | 86.5% [82.8%, 89.5%] | 85.8% [82.0%, 88.8%] | 0 |
| jeff.retrieval.ground | 96.6% | 94.8% [92.1%, 96.5%] | 95.0% [91.3%, 97.2%] | 178 |
| jeff.routing.tools | 97.2% | 97.8% [95.8%, 98.8%] | 97.8% [95.8%, 98.8%] | 0 |
| jeff.safety.guard | 98.2% | 97.5% [95.5%, 98.6%] | 97.2% [95.1%, 98.5%] | 0 |
| jeff.safety.spam | 98.1% | 98.8% [97.1%, 99.5%] | 98.8% [97.1%, 99.5%] | 0 |
| jeff.security.soc | 94.1% | 94.8% [92.1%, 96.5%] | 94.8% [92.1%, 96.5%] | 0 |
| jeff.support.support-intents | 96.3% | 95.0% [92.4%, 96.7%] | 95.0% [92.4%, 96.7%] | 0 |
| jeff.support.triage | 91.5% | 93.5% [90.6%, 95.5%] | 94.0% [91.2%, 95.9%] | 0 |
| jeff.voice.nav | 97.3% | 96.5% [94.2%, 97.9%] | 97.1% [94.5%, 98.5%] | 92 |
