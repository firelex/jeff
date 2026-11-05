# Catalogue imports for the private adapters

One hand-written file per private adapter in `teamsuzie/jeff-adapters`. It says what the
adapter's rows cannot: the adapter's ID and version, what its context and input fields mean, and which interface each
function implements. Everything else (instructions, labels, base, files) is read from Hugging Face by
`scripts/import_private.py`.

```yaml
path: apps/trading-desk            # the folder in teamsuzie/jeff-adapters and teamsuzie/jeff-data
id: jeff.finance.trading-desk
aliases: [trading-desk]
version: 1.4.0
function_by: id_suffix             # how a test row names its function: see below
context: [{key: desk, description: "..."}]
input: {key: market_state, description: "...", form: fields}   # fields | text
functions: {route: {interface: classify}, risk_off: {interface: classify}}
```

Every field above is required. `aliases` holds short names, and for an adapter whose ID carries its base's version (`jeff.routing.router-open-v14`) also that ID without the version part (`jeff.routing.router-open`), the name the docs use (the rule is the manifest schema's; moving such an alias to a new base's adapter is a release step, done only when the new adapter's interface is the same). `context` lists the state fields before the input, in the rows' order; `input` is the
last state field. A context entry may add `required: false` when some rows leave that field out (the import checks
that every row's fields are the listed ones, in order, with only such fields missing). `input.form` is `fields` when
the rows' state is an object of named fields, and `text` when every row's state is one plain string (the adapter was
trained on plain text; Jeff then sends the caller's input unwrapped, and `context` must be empty). A key is written
exactly as the rows have it, spaces included ("outreach log"); callers use its code name (`outreach_log`, derived by
the manifest's rule), and a context entry or the input may declare its own `code_name`. The interfaces are those of
`contract/interfaces.yaml`.

`function_by` is the manifest's `tests.function_by`: `only` (one function), or one of the criteria `id_suffix` (the
row id ends with `-` or `:` and the function key), `question_type`, `source_kind` (the row's `source.kind`),
`state_shape` (the row's state keys, in order, are the function's context keys and then its input key, or a plain
string for a text input) and `id_pattern` (the row id matches the function's regular expression), or a list of
criteria applied in order, each keeping the functions it allows; exactly one function must remain for every row.

A function may say more than its interface:
- `context` and `input` (both, never one): its own request shape, for an adapter trained on several (its rows'
  state is checked against these instead of the adapter's);
- `labels: {fixed: [keys in order], subset: true | false}`: fixed choice labels in this order, when the rows do not
  settle it (training shuffled them); `subset: true` lets a caller offer some of them, and is required when some rows
  offer fewer than all. Every key the test rows offer must be listed, each with one text.
- `option_order: shuffled`: training offered this function's options in varying orders from row to row, so the
  manifest's order cannot be the one order training used. Without it the import compares the options of every test
  and calibration row, in order, with the options the manifest rebuilds for that row, and stops naming the first row
  that differs; with it, it checks that some row really does differ (else it stops: leave `option_order` out). Fixed
  labels shuffled in training are listed by key, or in the order `labels` gives; caller labels whose always-offered
  options (`fixed_first`) came at varying positions need it too.

Optional fields:
- `adapter_path`: the adapter folder, when it is not at `path` (for example its folder under `--adapters-root`);
- `data_revision`: the `teamsuzie/jeff-data` revision the rows are read at, when not `main` (an adapter trained on a
  data version that `main` has since replaced);
- `kinds`: the manifest's `tests.kinds`, with `source_kind`: each `source.kind` value -> its function key, or a list
  of keys when one kind holds rows of several functions (another criterion must then tell them apart);
- `id_patterns`: the manifest's `tests.id_patterns`, required with `id_pattern`: every function key, in order -> a
  regular expression its row ids match as a whole (for ids that do not end with the function key);
- `access`: the manifest's `access`, who may call the adapter on a Jeff server: `owner` (the owner's key only), or
  `{accounts: [<hub account id>, ...]}` (the owner and those accounts). Absent: everyone. See `deploy/README.md`,
  "Private adapters".

The v1.4 adapters' import files are in `v1.4/` (`--imports catalogue-imports/v1.4`).

## Running it

```
uv run python scripts/import_private.py trading [more names] --out <folder>
```

It only reads Hugging Face (with the token configured on the machine): it lists the adapter folder, downloads its
`decision_config.json` and `README.md`, and downloads `test.jsonl` and `calibration.jsonl` from the same path in the
dataset `teamsuzie/jeff-data`. The weights are never downloaded. It writes `<folder>/<id>/manifest.yaml`:

- licence proprietary (Scissero), weights `{proprietary: {repo: teamsuzie/jeff-adapters, path: <path>}}`;
- the base found by the SHA-256 of the base weights the adapter's `decision_config.json` records, matched against
  `catalogue/bases.yaml`;
- each function's instructions: the one text all its test rows use; its labels: read from its test rows (fixed labels
  in the one order every row offers them in, as training used it; by key when training shuffled them, which the
  import file must say with `option_order: shuffled`; in the interface's order for `is_supported` and `equivalent`;
  labels drawn from a set larger than the base allows become caller labels, with the labels every row offers first);
- `tests.internal` and `tests.internal_calibration` pointing at the private files, `tests.published` null, and every
  function's `calibration` null until `jeff calibrate` fills it;
- `hub.notes` recording each rule the rows alone did not settle.

Rows that record how their instructions were written (`source.instructions`: `canonical`, a rewording, or `none`, as
the v1.4 data does) give each function the one text of its rows marked canonical; the others ask the same question in
other words. Rows with no mark among marked ones must use that canonical text word for word. The import also stops
when a row of either set is one the manifest cannot describe (the template mode `jeff calibrate` and the gate use
skips such rows), when a rebuilt row differs from the row as trained (its state, its options or their order, or the
instructions of a row marked canonical or unmarked), or when the calibration and test sets share a row (an id, or a state and question).

**Adapters not on Hugging Face** (the v1.4 adapters, on the RTX only): `--adapters-root <folder>` reads each adapter
folder (`adapter_path` under it) from disk instead of `teamsuzie/jeff-adapters`, and the manifest's weights are
`{local: {}}`: the folder holding the manifest, which an engine serves with `--adapters-dir`. When the folder holds only
copies of the small files (decision_config.json and README.md, pulled from the RTX), `--adapters-listing <file>` names
every file of the real folders, one path per line relative to the root (`find <root> -type f -printf '%P\n'`), so the
weight files are still checked. A base kept only on the RTX is listed in `catalogue/bases.yaml` by `path` and
`model_sha256`, and matched by that SHA-256.

```
uv run python scripts/import_private.py trading --imports catalogue-imports/v1.4 \
  --adapters-root <copy of /mnt/d/jeff-adapters/v1.4> --adapters-listing <listing> --out results/v14-manifests
```

It stops, naming the adapter and the function, when the rows and the import file disagree (a function in one and not
the other, a state shape that differs or is mixed, an interface that is not in the contract or compiles to another
question type), when the folder lacks an adapter file, or when the rows do not settle the labels.

Until the adapters are retrained (owner, 2026-10-22), manifests from here are not committed to `catalogue/`.
