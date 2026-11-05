---
license: apache-2.0
base_model: mstrasser/jeff-base
base_model_relation: adapter
library_name: peft
pipeline_tag: text-classification
language:
- en
tags:
- jeff
- lora
- decision-model
- calibration
- legal
---

# jeff-adapter-legal-clauses

**Contract clause classification.** Classifies a single contract provision by clause type, such as governing law, notices or confidentiality.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.legal.legal-clauses`, version 1.4.0; short names: `legal-clauses`
- Catalogue page: [jeffhub.ai/catalogue/jeff.legal.legal-clauses](https://jeffhub.ai/catalogue/jeff.legal.legal-clauses)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You have split a contract into provisions and need a type for each one.
- Your input consists of commercial or employment agreements in English.

## When not to use it

- You need legal advice or an assessment of fairness or enforceability; this adapter only identifies the clause type.
- You need to process an entire contract in one call; supply one provision at a time.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `document` (context) | yes | What the provision comes from, in a few words (for example "A commercial contract filed with the U.S. Securities and Exchange Commission (SEC)"). |
| `provision` (input, fields) | yes | One provision (a clause) of a contract, as plain text. |

### `jeff.legal.legal-clauses.clause_type` (classify, choice)

Instructions: Which type of clause is this contract provision? Choose the clause type that best matches the main topic of the provision.

Labels:

  - `adjustments`: Adjustments: how amounts, prices, share numbers or other terms are adjusted when stated events happen (for example a stock split).
  - `agreements`: Agreements: a provision headed 'Agreements', stating what the parties agree to or which other agreements apply.
  - `amendments`: Amendments: how the contract can be amended, usually only in writing signed by the parties.
  - `anti_corruption_laws`: Anti-corruption laws: promises to comply with bribery and corruption laws such as the U.S. Foreign Corrupt Practices Act.
  - `applicable_laws`: Applicable laws: which laws apply to the contract or to the parties' conduct, or a promise to follow applicable laws.
  - `approvals`: Approvals: approvals that are needed or have been obtained (from boards, shareholders, regulators or the other party).
  - `arbitration`: Arbitration: disputes are settled by arbitration, with the rules, place and procedure for it.
  - `assignments`: Assignments: whether and how a party may transfer its rights or duties under the contract to someone else.
  - `assigns`: Assigns: the contract binds and benefits the parties' permitted assigns (those to whom rights are transferred).
  - `authority`: Authority: a party states it has the power and authority to enter into and perform the contract.
  - `authorizations`: Authorizations: the contract has been duly authorized by the necessary corporate action, or needed permits are held.
  - `base_salary`: Base salary: the employee's fixed yearly pay and how it is paid or reviewed.
  - `benefits`: Benefits: employee benefits such as health insurance, retirement plans and other benefit programs.
  - `binding_effects`: Binding effect: the contract is binding on, and benefits, the parties and their successors.
  - `books`: Books: keeping accurate books of account and records, and access to inspect them.
  - `brokers`: Brokers: no broker or finder is owed a fee for the deal, or who pays any such fee.
  - `capitalization`: Capitalization: the company's authorized and issued shares and other equity.
  - `change_in_control`: Change in control: what happens when ownership or control of a party changes (for example a merger or sale).
  - `closings`: Closings: when and where the deal closes and what is delivered at closing.
  - `compliance_with_laws`: Compliance with laws: a party complies, or will comply, with all laws that apply to it.
  - `confidentiality`: Confidentiality: keeping information secret and limits on using or disclosing it.
  - `consent_to_jurisdiction`: Consent to jurisdiction: the parties consent to the jurisdiction of named courts for disputes.
  - `consents`: Consents: consents of third parties or authorities that are needed or have been obtained, or how consent may be given.
  - `construction`: Construction: rules for reading and interpreting the contract (for example no presumption against the drafter).
  - `cooperation`: Cooperation: the parties will cooperate and assist each other to carry out the contract.
  - `costs`: Costs: who bears costs and expenses, such as legal costs or costs of enforcement.
  - `counterparts`: Counterparts: the contract may be signed in separate copies, including electronic signatures, which together form one agreement.
  - `death`: Death: what happens under the contract if the employee or a party dies.
  - `defined_terms`: Defined terms: states that capitalized terms have the meanings defined elsewhere, or lists defined terms.
  - `definitions`: Definitions: a list of terms and the meanings they have in the contract.
  - `disability`: Disability: what happens if the employee becomes disabled and unable to work, and how disability is defined.
  - `disclosures`: Disclosures: information that has been or must be disclosed, or statements that disclosures are accurate and complete.
  - `duties`: Duties: the duties and responsibilities of a person's role (for example an employee's or officer's duties).
  - `effective_dates`: Effective date: the date on which the contract or a provision takes effect.
  - `effectiveness`: Effectiveness: when and under what conditions the contract becomes effective.
  - `employment`: Employment: the employment relationship itself, such as the position, start and at-will terms.
  - `enforceability`: Enforceability: the contract is valid, binding and enforceable against the party, subject to usual exceptions.
  - `enforcements`: Enforcement: how the contract's rights and obligations can be enforced.
  - `entire_agreements`: Entire agreement: the contract is the whole agreement and replaces all earlier agreements on the subject.
  - `erisa`: ERISA: matters under the U.S. Employee Retirement Income Security Act, such as pension and benefit plan compliance.
  - `existence`: Existence: a party is duly organized, validly existing and in good standing, and will keep its existence.
  - `expenses`: Expenses: which party pays or reimburses which expenses.
  - `fees`: Fees: fees payable under the contract (for example commitment, agency or service fees).
  - `financial_statements`: Financial statements: the accuracy of financial statements, or the duty to deliver them.
  - `forfeitures`: Forfeiture: when awards, shares or rights are forfeited (lost), for example on leaving employment.
  - `further_assurances`: Further assurances: the parties will sign further documents and take further steps needed to carry out the contract.
  - `general`: General: a provision headed 'General' that holds assorted general terms.
  - `governing_laws`: Governing law: which state's or country's law governs the contract.
  - `headings`: Headings: section headings are for convenience only and do not affect the meaning.
  - `indemnifications`: Indemnification: one party must compensate and defend the other against losses and claims.
  - `indemnity`: Indemnity: a promise to hold another party harmless from losses, claims and liabilities.
  - `insurances`: Insurance: insurance a party must carry, or the insurance it has in place.
  - `integration`: Integration: the written contract fully integrates the parties' agreement and supersedes earlier understandings.
  - `intellectual_property`: Intellectual property: ownership, use or protection of patents, trademarks, copyrights and other intellectual property.
  - `interests`: Interests: interest on amounts owed, such as interest rates and how interest is calculated, or ownership interests.
  - `interpretations`: Interpretation: rules for interpreting words in the contract (for example 'including' means 'including without limitation').
  - `jurisdictions`: Jurisdiction: which courts have jurisdiction over disputes under the contract.
  - `liens`: Liens: limits on creating liens or security interests on assets, or statements that assets are free of liens.
  - `litigations`: Litigation: there are no pending or threatened lawsuits or proceedings, or how litigation is handled.
  - `miscellaneous`: Miscellaneous: a provision headed 'Miscellaneous' that gathers assorted final terms.
  - `modifications`: Modifications: how the contract can be changed or modified, usually only in writing.
  - `no_conflicts`: No conflicts: entering into the contract does not breach the party's charter, other contracts or the law.
  - `no_defaults`: No defaults: no default or event of default has happened or is continuing.
  - `no_waivers`: No waiver: failing or delaying to use a right does not waive that right.
  - `non_disparagement`: Non-disparagement: a party will not make negative or damaging statements about the other.
  - `notices`: Notices: how formal notices must be given, and to which addresses.
  - `organizations`: Organization: a party is duly organized and in good standing under the laws of its home jurisdiction.
  - `participations`: Participations: a lender may sell participations (shares) in its loans to other parties.
  - `payments`: Payments: how, when and where payments must be made.
  - `positions`: Position: the job title and position the employee holds.
  - `powers`: Powers: the powers of a party, agent or trustee, or a party's corporate power to act.
  - `publicity`: Publicity: limits on press releases and public announcements about the contract.
  - `qualifications`: Qualification: a party is qualified to do business in the jurisdictions where it operates.
  - `records`: Records: keeping records and giving access to them.
  - `releases`: Release: a party gives up (releases) claims it has against the other.
  - `remedies`: Remedies: the remedies available for a breach, and whether they are cumulative.
  - `representations`: Representations: statements of fact a party makes to the other, or how long such statements survive.
  - `sales`: Sales: the sale of goods, shares or assets, or limits on selling them.
  - `sanctions`: Sanctions: compliance with economic sanctions laws and dealings with sanctioned persons or countries.
  - `severability`: Severability: if one provision is invalid, the rest of the contract remains in force.
  - `solvency`: Solvency: a party is solvent, able to pay its debts, and will not become insolvent because of the deal.
  - `specific_performance`: Specific performance: a party may ask a court to order the other to perform, not just pay damages.
  - `submission_to_jurisdiction`: Submission to jurisdiction: the parties submit to the jurisdiction of named courts.
  - `subsidiaries`: Subsidiaries: the party's subsidiaries, their ownership, or duties that extend to them.
  - `successors`: Successors: the contract binds and benefits the parties' successors.
  - `survival`: Survival: which provisions continue to apply after the contract ends.
  - `tax_withholdings`: Tax withholding: amounts may be withheld from payments to cover required taxes.
  - `taxes`: Taxes: who pays which taxes, tax treatment, and tax gross-ups.
  - `terminations`: Termination: how and when the contract or employment can be ended, and what follows.
  - `terms`: Term: how long the contract or employment lasts, and renewal.
  - `titles`: Title: ownership of property or assets, and when title passes; or a person's job title.
  - `transactions_with_affiliates`: Transactions with affiliates: limits on deals with affiliated companies, which must be on fair terms.
  - `use_of_proceeds`: Use of proceeds: what the money raised or borrowed may be used for.
  - `vacations`: Vacation: the employee's paid vacation or time off.
  - `venues`: Venue: where (in which place or court) lawsuits must be brought.
  - `vesting`: Vesting: when options, shares or benefits vest (become the holder's own), and the schedule.
  - `waiver_of_jury_trials`: Waiver of jury trial: the parties give up the right to a jury trial in disputes.
  - `waivers`: Waiver: how rights under the contract can be waived, usually only in writing.
  - `warranties`: Warranties: promises that stated facts are true or that goods or services meet stated standards.
  - `withholdings`: Withholding: payments are subject to withholding of required taxes and other deductions.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| real contract clauses filed with the US SEC, labelled with LEDGAR's 100 clause types (real) | 9,570 | 84.2% |
| clause types or wordings new to training (real) | 7,199 | 82.1% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `clause_type` | 0.805 | 95.0% | 67.8% | 922 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Governing law clause". The provision names the law that governs the contract, so its clause type is `governing_laws`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["legal-clauses"])

decide = client.function("jeff.legal.legal-clauses.clause_type", context={
    "document": "A commercial contract filed with the U.S. Securities and Exchange Commission (SEC)",
})

decision = decide("This Agreement and any dispute arising out of or in connection with it shall be governed by and construed in accordance with the laws of the State of Delaware, without regard to its conflict of laws principles.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.legal.legal-clauses.clause_type", context={
        "document": "A commercial contract filed with the U.S. Securities and Exchange Commission (SEC)",
    })

    decision = decide("This Agreement and any dispute arising out of or in connection with it shall be governed by and construed in accordance with the laws of the State of Delaware, without regard to its conflict of laws principles.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.legal.legal-clauses.clause_type", "input": "This Agreement and any dispute arising out of or in connection with it shall be governed by and construed in accordance with the laws of the State of Delaware, without regard to its conflict of laws principles.", "context": {"document": "A commercial contract filed with the U.S. Securities and Exchange Commission (SEC)"}}]}'
```

Recorded answer (2026-11-03): `governing_laws` with confidence 0.93.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-legal-clauses --revision v1.4 --local-dir adapters/legal-clauses
```

## Files

| File | What it is |
|---|---|
| `adapter_model.safetensors`, `adapter_config.json` | The LoRA weights and their configuration |
| `readout.safetensors` | The adapter's readout over the answer codes |
| `decision_config.json` | Answer codes, temperature, prompt layout and the base it was trained on (the SHA-256 of the base's weights, which the engine checks) |
| `manifest.yaml` | The adapter's manifest: its functions, labels, fields and calibration, as the SDKs read it |
| `test.jsonl` | The held-out test set the numbers above come from |
| `calibration.jsonl` | The calibration set the thresholds come from |
| `LICENSE` | The Apache License 2.0, for the Qwen3.5-0.8B notice below |

## Licence

Adapter licence: **Apache-2.0**

**Qwen3.5-0.8B notice:** these weights were modified from [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) by the Jeff project: jeff-base is a fine-tune of Qwen3.5-0.8B, and this adapter was trained on top of it. Qwen3.5-0.8B is Copyright 2026 Alibaba Cloud and licensed under the Apache License, Version 2.0; a copy of that licence is in [`LICENSE`](LICENSE).

Jeff is an independent project, not affiliated with or endorsed by the makers of Qwen.
