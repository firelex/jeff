"""Record contract/golden/step1.json: one case per function of every catalogue adapter.

Each case's call is built, in order of preference:
1. From the adapter's example.json (state -> context + input, criteria -> labels for the function's label form).
2. When example.json does not show that function, or its state does not fit the manifest, from the first row (in
   file order) of the adapter's test set (tests.published, else tests.internal) that the manifest can rebuild as a
   Call.

Every case records its decision from Jeff.local and where its call came from, so step 2 (server) and step 3 (Node)
can replay the same file. Run once now; afterwards golden cases change only deliberately."""

import json
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download

from jeff import Call
from jeff.binding import Labels
from jeff.catalogue import Catalogue
from jeff.errors import JeffError
from jeff.evaluate import _labels, read_rows, split_state, template_rows
from jeff.local import local_for_batches
from jeff.manifest import FunctionSpec, Manifest

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION = ("One case per catalogue function, recorded from Jeff.local by scripts/record_golden.py. Every mode "
               "(local, server, Node) must reproduce each case: value must be the same (and of the same JSON type); "
               "confidence must be within tolerance of expected.confidence; abstained must match, except that "
               "abstained may differ when |expected.confidence - the function's calibration threshold| <= tolerance, "
               "because a confidence that may move by the tolerance can then fall on either side of the threshold. "
               "Recorded values are never edited by hand.")


def _call_dict(function: FunctionSpec, input: Any, context: dict[str, Any], labels: Labels | None) -> dict[str, Any]:
    return {"function": function.id, "input": input, "context": context, "labels": labels}


def _from_example(manifest: Manifest, function: FunctionSpec, example: dict[str, Any]
                  ) -> dict[str, Any] | None:
    """The call built from example.json's state and this function's question, or None when example.json does not
    show the question or its state does not fit the function (evaluate.py's own split_state check, also used to
    rebuild test rows)."""
    question = example["questions"].get(function.key)
    if question is None or question["type"] != function.type:
        return None
    split = split_state(function, example["state"])
    if split is None:
        return None
    context, input = split
    labels, reason = _labels(function, question.get("criteria"))
    if reason is not None:
        return None
    return _call_dict(function, input, context, labels)


def _from_test_set(manifest: Manifest, function: FunctionSpec) -> tuple[dict[str, Any], str]:
    """The first row (in file order) of the function's test set that the manifest can rebuild as a Call, and its
    row id, for the "source" field."""
    raws = read_rows(manifest, "test")
    rows, _ = template_rows(manifest, raws)
    for row in rows:
        if row.function == function.id:
            call = row.call
            return _call_dict(function, call.input, dict(call.context or {}), call.labels), row.id
    raise JeffError(f"{function.id}: no row of its test set (read in file order) is one the manifest can rebuild")


def main() -> None:
    catalogue = Catalogue.from_dir(ROOT / "catalogue")
    cases = []
    for manifest in catalogue.manifests():
        weights = manifest.weights["open"]
        folder = Path(snapshot_download(weights["repo"], revision=weights["revision"]))
        example = json.loads((folder / "example.json").read_text())
        jeff = local_for_batches([manifest.id], rows_per_pass=1, catalogue=catalogue)  # one call per decide below
        for key in manifest.functions:
            function = manifest.functions[key]
            call = _from_example(manifest, function, example)
            source = "example.json"
            if call is None:
                call, row_id = _from_test_set(manifest, function)
                source = f"test row {row_id}"
            decision = jeff.decide([Call(**call)])[0]
            cases.append({
                "name": f"{function.id} ({source})",
                "source": source,
                "call": call,
                "expected": {"value": decision.value, "abstained": decision.abstained,
                             "confidence": decision.confidence},
                "tolerance": 0.03,
            })
        del jeff
    target = ROOT / "contract" / "golden" / "step1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"description": DESCRIPTION, "cases": cases}, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(cases)} cases -> {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
