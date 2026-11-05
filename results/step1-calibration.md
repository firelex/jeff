# Step 1 calibration

`jeff calibrate <adapter> --target-precision 0.95` run once per adapter (13 adapters, 23 functions), each against its
own calibration set (`tests.calibration` for 9 adapters, `tests.internal_calibration` for the other 4 — aml, sanctions,
trading-desk, soc — which have no public calibration set; `--split-test` was not needed for any adapter). Target
precision 0.95 is the controller's starting value from the spec's example, not a measured requirement; the owner may
change it later. Every function reached 0.95 — none was unreachable at this target.

| Function | Rows | Threshold | Precision | Coverage |
|---|---|---|---|---|
| jeff.compliance.aml.action | 546 | 0.506 | 95.0% | 99.6% |
| jeff.compliance.aml.suspicious | 546 | 0.506 | 95.6% | 100.0% |
| jeff.compliance.sanctions.screening | 1012 | 0.596 | 99.8% | 100.0% |
| jeff.finance.trading-desk.direction | 200 | 0.930 | 100.0% | 100.0% |
| jeff.finance.trading-desk.order | 200 | 0.999 | 100.0% | 100.0% |
| jeff.finance.trading-desk.risk_off | 200 | 0.999 | 100.0% | 100.0% |
| jeff.finance.trading-desk.route | 200 | 0.480 | 95.0% | 100.0% |
| jeff.finance.trading-desk.size | 200 | 0.783 | 95.1% | 82.0% |
| jeff.language.emotion.emotion | 1000 | 0.905 | 96.2% | 7.8% |
| jeff.legal.legal-clauses.clause_type | 958 | 0.777 | 95.1% | 69.6% |
| jeff.retrieval.ground.grounding | 720 | 0.385 | 97.6% | 100.0% |
| jeff.routing.tools.tool | 2195 | 0.236 | 97.3% | 100.0% |
| jeff.safety.guard.attempt | 1097 | 0.521 | 99.1% | 100.0% |
| jeff.safety.guard.kind | 1097 | 0.481 | 98.1% | 100.0% |
| jeff.safety.spam.is_spam | 1113 | 0.549 | 98.8% | 100.0% |
| jeff.safety.spam.message_type | 832 | 0.376 | 97.2% | 100.0% |
| jeff.security.soc.action | 991 | 0.507 | 96.1% | 100.0% |
| jeff.support.support-intents.intent | 999 | 0.219 | 96.8% | 100.0% |
| jeff.support.triage.needs_human | 367 | 0.535 | 97.8% | 100.0% |
| jeff.support.triage.route | 367 | 0.405 | 95.1% | 100.0% |
| jeff.support.triage.sentiment | 367 | 0.693 | 95.2% | 85.6% |
| jeff.support.triage.urgency | 367 | 0.772 | 95.2% | 57.2% |
| jeff.voice.nav.target | 1516 | 0.283 | 96.8% | 100.0% |

Unreachable functions (none at target 0.95): none. Every function's calibration set reached precision ≥ 0.95 at some
threshold, so no function was left uncalibrated (`calibration: null`) and no `jeff calibrate` run exited non-zero.

Notes on coverage:
- `jeff.language.emotion.emotion` only reaches 95%+ precision at 7.8% coverage (threshold 0.905) — this adapter's raw
  accuracy is ~60% (see `results/step1-accuracy.md`), the weakest of the 13, so most decisions abstain once calibrated
  at this target. This is a legitimate result of the fit, not a bug; the owner may want a lower target for this adapter.
- `jeff.finance.trading-desk.size`, `jeff.legal.legal-clauses.clause_type`, `jeff.support.triage.sentiment` and
  `jeff.support.triage.urgency` reach 0.95 only at reduced coverage (57%-86%); their other functions on the same
  adapter stay near 100% coverage, since calibration is per function.

Rows skipped (shapes the calibration set has that the manifest does not describe, not scored):
- jeff.compliance.aml: 44 rows (maps to no function)
- jeff.retrieval.ground: 720 rows (no input field) — the calibration set has one row per candidate passage; only the
  720 rows with a single input field were used, matching the function's single-input `grounding` question.
- jeff.voice.nav: 478 rows (17 fixed_first options missing, 312 no input field, 149 state fields differ from the
  manifest)
- All other adapters: 0 rows skipped.

After calibration, `jeff test` should be run on the complement of this data (the published/internal test set, not the
calibration set) to measure accuracy without reusing the rows calibration was fit on.
