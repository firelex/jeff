"""Runs jeff.contract (the Python SDK) on many documents and writes what it says to contract-cases.json, next to this
file, for ContractTest to compare the Java SDK with. Run it from packages/python of the repository:
    uv run python ../java/src/test/resources/contract-cases.py
The output is the same whatever PYTHONHASHSEED is (no case depends on the order of a Python set)."""
import copy
import json
import sys

import yaml
from jeff import contract
from jeff.errors import ContractError

from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
manifest = json.loads(json.dumps(yaml.safe_load(open(f"{REPO}/catalogue/jeff.safety.spam/manifest.yaml")), default=str))
del manifest["hub"]  # optional catalogue text, left out to keep the cases small
assert contract.is_valid("manifest", manifest), contract.first_problem("manifest", manifest)
fn_key = next(iter(manifest["functions"]))

cases = []


def case(label, schema, document, definition=None):
    out = {"label": label, "schema": schema, "definition": definition, "document": document}
    try:
        contract.validate(schema, document, "the document", definition)
        out["validate"] = None
    except ContractError as error:
        out["validate"] = str(error)
    out["valid"] = out["validate"] is None
    if definition is None:
        assert contract.is_valid(schema, document) == out["valid"]
    try:
        out["first_problem"] = contract.first_problem(schema, document, definition)
    except ContractError as error:
        out["first_problem_error"] = str(error)
    cases.append(out)


def edit(base, change):
    doc = copy.deepcopy(base)
    change(doc)
    return doc


def fn(doc):
    return doc["functions"][fn_key]


decision = {"value": "a", "confidence": 0.5, "abstained": False, "calibrated": True, "interface": "i", "function": "f",
            "version": "1", "probabilities": {"a": 0.5}, "score": None, "route": [], "decided_by": "jeff",
            "escalations": [], "timing": {}, "evidence_refs": []}
assert contract.is_valid("decision", decision)

# --- the brief's cases
case("decision with only a value", "decision", {"value": 1})
case("predict without rows (definition)", "socket", {"op": "predict", "id": 1, "adapter": "a"}, "predict")
case("predict with no rows (definition)", "socket", {"op": "predict", "id": 1, "adapter": "a", "rows": []}, "predict")
case("predict valid (definition)", "socket", {"op": "predict", "id": 1, "adapter": "a", "rows": [{}]}, "predict")
case("predict without rows", "socket", {"op": "predict", "id": 4, "adapter": "a"})
case("stats with an extra key", "socket", {"op": "stats", "id": 4, "extra": 1})
case("unknown op", "socket", {"op": "nope", "id": 3})
case("installed with id 1.0", "socket", {"op": "installed", "id": 1.0}, "simple")
case("on_low_confidence maybe", "decide_request", {"calls": [{"function": "f", "input": "x"}], "on_low_confidence": "maybe"})
case("labels 5 in three calls", "decide_request", {"calls": [{"function": "f", "input": "x", "labels": 5}] * 3})
case("empty decide_response", "decide_response", {"decisions": []})

# --- required, additionalProperties, type, enum, const
case("several required missing", "decision", {})
case("extras sorted by code point", "socket", {"op": "stats", "id": 4, "zeta": 1, "Alpha": 2, "beta": 3}, "simple")
case("one extra", "login_request", {"zzz": 1})
case("type list", "decision", edit(decision, lambda d: d.update(value=1.5)))
case("type of root", "decision", [1, 2])
case("type of root string", "decision", "text")
case("type null", "decision", None)
case("bool is not an integer", "socket", {"op": "installed", "id": True}, "simple")
case("float is not an integer", "socket", {"op": "installed", "id": 1.5}, "simple")
case("big int", "socket", {"op": "installed", "id": 2 ** 70}, "simple")
case("enum", "socket", {"op": "nope", "id": 1}, "simple")
case("enum of numbers", "systemone_request", {"model": "m", "state": "s", "questions": {"q": {"type": "noul"}}, "orders": 3})
case("enum True is not 1", "systemone_request", {"model": "m", "state": "s", "questions": {"q": {"type": "noul"}}, "orders": True})
case("enum 1.0 is 1", "systemone_request", {"model": "m", "state": "s", "questions": {"q": {"type": "noul"}}, "orders": 1.0})
case("const", "socket", {"op": "hello", "version": 1}, "predict")
case("const True vs 1", "socket", {"id": 1, "ok": 1, "result": {}}, "ok_reply")
case("const valid", "socket", {"id": 1, "ok": True, "result": {}}, "ok_reply")

# --- nested arrays and objects
case("graph outputs with numbers", "graph", {"graph": "g", "version": "1.0.0", "input": "x", "params": [],
                                             "nodes": {"a": {"call": "f"}}, "outputs": [1, "a", 2]})
case("decision route item missing keys", "decision", {"value": "a", "confidence": 0.5, "abstained": False,
                                                       "calibrated": True, "interface": "i", "function": "f",
                                                       "version": "1", "probabilities": {"a": 2, "b": -1},
                                                       "score": None, "route": [{"router": "r"}, 5],
                                                       "decided_by": "jeff", "escalations": [],
                                                       "timing": {}, "evidence_refs": ["x", 3]})
items = [{"function": "f", "input": "x"}] * 12
items = items[:2] + [{"function": "f", "input": "x", "labels": 5}] + items[3:10] + [{"input": "x"}] + items[11:]
case("index 10 sorts before index 2 in first_problem", "decide_request", {"calls": items})

# --- oneOf with zero and several matches
graph = {"graph": "g", "version": "1.0.0", "input": "x", "params": [], "nodes": {"a": {"call": "f"}}, "outputs": ["a"]}
case("graph valid", "graph", graph)
case("node with neither call nor code", "graph", edit(graph, lambda d: d["nodes"].update(a={"after": []})))
case("node with both call and code", "graph", edit(graph, lambda d: d["nodes"].update(a={"call": "f", "code": "c"})))
case("condition matching nothing", "graph", edit(graph, lambda d: d["nodes"]["a"].update(when={"node": "a"})))
case("condition with a bad leaf deep inside", "graph", edit(graph, lambda d: d["nodes"]["a"].update(
    when={"all": [{"node": "a", "is": True}, {"not": {"any": [{"node": "b", "in": []}]}}]})))
case("condition with a float is", "graph", edit(graph, lambda d: d["nodes"]["a"].update(when={"not": {"node": "a", "is": 1.5}})))
case("hello matching engine_hello only", "socket", {"op": "hello", "version": 1, "adapters_version": 0})
case("hello", "socket", {"op": "hello", "version": 1})
case("hello wrong version type", "socket", {"op": "hello", "version": "1"})
case("labels both object and nothing", "decide_request", {"calls": [{"function": "f", "input": "x", "labels": {"a": 1}}]})
case("dispatch shape missing interface", "decide_request", {"dispatch": {"input": "x"}})
case("graph by id empty", "decide_request", {"graph": {"id": ""}, "input": 1, "params": {}})
case("policy escalate empty", "decide_request", {"calls": [{"function": "f", "input": 1}], "on_low_confidence": {"escalate": []}})
case("policy budget zero", "decide_request", {"calls": [{"function": "f", "input": 1}],
                                              "on_low_confidence": {"escalate": ["a"], "budget_ms": 0}})
case("root not an object", "decide_request", [])
case("no shape at all", "decide_request", {})
case("two shapes", "decide_request", {"calls": [{"function": "f", "input": 1}], "dispatch": {"input": 1, "interface": "i"}})

# --- patterns
for version in ["1.2", "1.2.3", "١.٢.٣", "1.2.3\n", "1.2.3\r", "1.2.3 ", "1.2.3\n\n", "a1.2.3"]:
    case(f"graph version {version!r}", "graph", edit(graph, lambda d: d.update(version=version)))
for name in ["jeff.a.b", "jeff.a", "abc\n", "abc\u0085", "Abc", "jeff.a-b.c_d", "a\nb"]:
    case(f"graph name {name!r}", "graph", edit(graph, lambda d: d.update(graph=name)))
for key in ["counterparty name", " leading", "trailing　", "tab\there", "line ", "oké", "x\n", "﻿bom"]:
    case(f"input key {key!r}", "manifest", edit(manifest, lambda d: d["input"].update(key=key)))
case("models release date", "models_response", {"models": [{"release_date": "2026-1-01"}]})
case("image data url", "systemone_request", {"model": "m", "state": "s", "questions": {"q": {"type": "noul"}},
                                              "images": ["data:image/gif;base64,AAAA", "data:image/png;base64,"]})
case("caller keys pattern", "manifest", edit(manifest, lambda d: fn(d)["labels"].update(caller_keys="x{m}")))
case("sha256 pattern", "manifest", edit(manifest, lambda d: fn(d)["calibration"].update(
    data_mirror={"path": "p", "sha256": "ABC"})))

# --- min/max, lengths, counts
case("minLength one", "socket", {"op": "predict", "id": 1, "adapter": "", "rows": [{}]}, "predict")
case("minimum", "socket", {"op": "predict", "id": 0, "adapter": "a", "rows": [{}]}, "predict")
case("exclusiveMinimum", "socket", {"op": "predict", "id": 1, "adapter": "a", "rows": [{}], "deadline_ms": 0}, "predict")
case("exclusiveMinimum negative float", "socket", {"op": "predict", "id": 1, "adapter": "a", "rows": [{}], "deadline_ms": -0.5}, "predict")
case("maximum", "manifest", edit(manifest, lambda d: d["base"].update(max_options=256)))
case("minimum 2", "manifest", edit(manifest, lambda d: d["base"].update(max_options=1)))
case("max float threshold", "manifest", edit(manifest, lambda d: fn(d)["calibration"].update(threshold=1.0000001)))
case("maxItems with count", "socket", {"op": "call_many", "id": 1, "handle": 1, "inputs": list(range(257))}, "call_many")
case("maxItems at root of decide", "decide_request", {"calls": [{"function": "f", "input": 1}] * 257})
case("maxProperties", "decide_request", {"graph": {"id": "g"}, "input": 1, "params": {},
                                         "on_low_confidence_by_node": {f"n{i}": "return" for i in range(257)}})
case("maxProperties 1", "manifest", edit(manifest, lambda d: d.update(weights={"open": {"repo": "r", "revision": "v"},
                                                                                 "local": {}})))
case("minProperties 1", "manifest", edit(manifest, lambda d: d.update(weights={})))
case("minItems 2", "manifest", edit(manifest, lambda d: fn(d)["labels"].update(levels=["a"])))
case("maxItems 10", "manifest", edit(manifest, lambda d: fn(d)["labels"].update(levels=list("abcdefghijk"))))
case("maxLength", "manifest", edit(manifest, lambda d: d["input"].update(key="k" * 61)))
case("maxLength images", "systemone_request", {"model": "m", "state": "s", "questions": {"q": {"type": "noul"}},
                                                "images": ["x"] * 5})
case("long message cut at 200", "decision", edit(decision, lambda d: d.update(value=["v" * 300])))
case("long unicode message cut", "decision", edit(decision, lambda d: d.update(value=["\U0001F600" * 300])))
case("repr escapes", "decision", edit(decision, lambda d: d.update(value=["tab\there", "quote'", "both'\"", "nl\n", "\u2028", "\x7f", "\U0001F600", "\ud800"])))

# --- uniqueItems, dependentRequired, if/then/else, not, propertyNames, allOf
case("uniqueItems", "manifest", edit(manifest, lambda d: d["tests"].update(function_by=["id_suffix", "id_suffix"])))
case("uniqueItems in kinds", "manifest", edit(manifest, lambda d: d["tests"].update(kinds={"k": ["a", "a"]})))
case("propertyNames pattern", "manifest", edit(manifest, lambda d: d["functions"].update({"Bad-Key": fn(d)})))
case("propertyNames minLength", "manifest", edit(manifest, lambda d: d["tests"].update(kinds={"": "a"})))
case("dependentRequired", "manifest", edit(manifest, lambda d: fn(d).update(context=[])))
case("if then: fixed choice needs subset", "manifest", edit(manifest, lambda d: (fn(d).update(type="choice"),
                                                                                   fn(d).update(labels={"source": "fixed", "fixed": {"a": None}}))))
case("else: not subset", "manifest", edit(manifest, lambda d: (fn(d).update(type="noul"),
                                                               fn(d).update(labels={"source": "fixed", "subset": True}))))
case("systemone choice without criteria", "systemone_request", {"model": "m", "state": "s",
                                                                 "questions": {"q": {"type": "choice"}}})
case("systemone score with extra", "systemone_request", {"model": "m", "state": "s",
                                                          "questions": {"q": {"type": "score", "criteria": ["a"], "x": 1}}})
case("systemone unknown type", "systemone_request", {"model": "m", "state": "s", "questions": {"q": {"type": "other"}}})
case("systemone noul criteria", "systemone_request", {"model": "m", "state": "s",
                                                       "questions": {"q": {"type": "noul", "criteria": {"maybe": "m"}}}})
case("inline graph allOf", "decide_request", {"graph": {"inline": edit(graph, lambda d: d.update(version="x"))},
                                              "input": 1, "params": {}})
case("inline graph too many nodes", "decide_request", {"graph": {"inline": edit(graph, lambda d: d.update(
    nodes={f"n{i}": {"call": "f"} for i in range(257)}))}, "input": 1, "params": {}})
case("functions_response allOf", "functions_response", {"functions": [{}]})

case("decision valid", "decision", decision)
case("decision confidence above 1 and nan score", "decision", edit(decision, lambda d: d.update(confidence=1.5, score=float("nan"))))
case("decision nan confidence", "decision", edit(decision, lambda d: d.update(confidence=float("nan"))))
case("decision inf confidence", "decision", edit(decision, lambda d: d.update(confidence=float("inf"))))
case("decision -inf confidence", "decision", edit(decision, lambda d: d.update(confidence=float("-inf"))))
case("decision probabilities", "decision", edit(decision, lambda d: d.update(probabilities={"z": 2, "a": -0.0, "b": -1e-300})))
case("decision value big float", "decision", edit(decision, lambda d: d.update(value=1e300)))
case("decision value 2.0", "decision", edit(decision, lambda d: d.update(value=2.0)))
case("decision value dict", "decision", edit(decision, lambda d: d.update(value={"k": [1, None, True]})))
case("many errors sorted", "decision", edit(decision, lambda d: d.update(route=[{"router": 1, "chose": 2, "confidence": "x", "q": 0}], abstained=None)))
case("socket predict rows bad items", "socket", {"op": "predict", "id": 1, "adapter": "a", "rows": [1, {}, "x"]})
case("socket failure reply nested", "socket", {"id": 1, "ok": False, "error": {"type": "T", "message": 3}})
case("socket bind labels", "socket", {"op": "bind", "id": 1, "function": "f", "labels": [1]})
case("socket graph policy by node", "socket", {"op": "graph", "id": 1, "graph": {"id": "g"}, "input": 1, "params": {},
                                                "on_low_confidence_by_node": {"a": "return", "b": "maybe"}})

# uniqueItems as jsonschema's uniq() decides it: sort, then compare neighbours. NaN documents are read from JSON text,
# as Java reads them: Python's json gives every NaN the same float object, so two NaNs are "the same" to equal().
for text in ['[NaN, NaN]', '[1, NaN, 1]', '["id_suffix", NaN, "id_suffix"]', '[NaN, 1, NaN]', '[1, 2, NaN, 2]',
             '[[1], [true], [1]]', '[true, true]', '[true, 1]', '[1, 1.0]', '[{"a": 1}, {"a": 1.0}]', '[null, null]',
             '[2, 1, 1, 3]', '[3, 2, 1, 1]', '[[1, NaN], [1, NaN]]', '[[NaN], [1], [NaN]]', '["b", "a", "b"]',
             '[' + ", ".join(str(i % 50) for i in range(70)) + ']', '[' + ", ".join('"k%d"' % i for i in range(70)) + ']']:
    case(f"uniqueItems {text[:40]}", "manifest", edit(manifest, lambda d: d["tests"].update(function_by=json.loads(text))))
    case(f"uniqueItems in kinds {text[:40]}", "manifest", edit(manifest, lambda d: d["tests"].update(kinds={"k": json.loads(text)})))

# --- every other schema at least once
for name in contract.SCHEMAS:
    case(f"{name}: empty object", name, {})
    case(f"{name}: a number", name, 7)
case("manifest valid", "manifest", manifest)
case("manifest licence kind", "manifest", edit(manifest, lambda d: d["licence"].update(kind="mine")))
case("graph_result", "graph_result", {"result": 1})
case("health", "health_response", {"status": "maybe"})
case("adapters", "adapters_response", {"adapters": [{"id": 1}]})

made_by = ("jeff.contract in packages/python (jsonschema " + __import__("importlib.metadata").metadata.version("jsonschema")
           + ", Python " + sys.version.split()[0] + "): validate (where = 'the document'), first_problem, and is_valid")
lines = ['{"made_by": ' + json.dumps(made_by) + ",", ' "cases": [',
         ",\n".join("  " + json.dumps(each, ensure_ascii=True) for each in cases), " ]", "}", ""]
(HERE / "contract-cases.json").write_text("\n".join(lines), encoding="utf-8")
