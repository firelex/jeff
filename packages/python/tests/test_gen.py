import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import jeff.cli as cli
from jeff import Decision
from jeff.catalogue import Catalogue
from jeff.errors import JeffError
from jeff.gen import class_name, python_module
from jeff.manifest import FunctionSpec, InputField, LabelSpec, Manifest

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
MESSAGE = InputField("message", "The message.", "message", "fields")


def test_generated_module_types_values(tmp_path: Path) -> None:
    source = python_module(Catalogue.from_dir(FIXTURES), ["spam", "triage"])
    assert "class SafetySpamMessageType" in source
    assert "Literal['legitimate', 'spam', 'phishing']" in source
    assert "Literal[0, 1, 2, 3, 4]" in source
    (tmp_path / "functions.py").write_text(source)
    (tmp_path / "use.py").write_text(
        "from functions import SafetySpamMessageType\n"
        "def f(value: SafetySpamMessageType.Value) -> None: ...\n"
        "f('ham')\n")
    result = subprocess.run([sys.executable, "-m", "mypy", "--strict", "use.py"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 1 and "ham" in result.stdout


def _manifest_with(function: FunctionSpec) -> Manifest:
    """A minimal manifest around a single hand-built function, for testing one label source or one collision case
    in isolation without touching the shared fixtures."""
    return Manifest(id=function.adapter_id, version="0.1.0", aliases=(), licence_kind="open", weights={},
                    base_id="jeff-base", base_version="v1.3", max_options=10, context=(),
                    input=MESSAGE, functions={function.key: function},
                    routes_to=(),
                    tests={"published": None, "calibration": None, "function_by": "only"}, raw={},
                    path=Path("test-manifest"))


def _catalogue_of(function: FunctionSpec) -> Catalogue:
    return Catalogue([_manifest_with(function)], bases={})


def test_subset_labels_are_typed_as_the_offered_set() -> None:
    function = FunctionSpec(
        adapter_id="jeff.test.subset", key="action", interface="classify", type="choice",
        instructions="Which action?",
        labels=LabelSpec(source="subset", fixed={"ignore": "Ignore it", "escalate": "Escalate it"},
                         required=("ignore",)),
        abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    source = python_module(_catalogue_of(function), ["jeff.test.subset.action"])
    assert "class TestSubsetAction" in source
    assert "Literal['ignore', 'escalate']" in source


def test_describe_labels_are_typed_from_keys() -> None:
    function = FunctionSpec(
        adapter_id="jeff.test.describe", key="route", interface="classify", type="choice",
        instructions="Which venue?",
        labels=LabelSpec(source="describe", keys=("venue_a", "venue_b")),
        abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    source = python_module(_catalogue_of(function), ["jeff.test.describe.route"])
    assert "class TestDescribeRoute" in source
    assert "Literal['venue_a', 'venue_b']" in source


def _decision(function_id: str, value: Any) -> Decision:
    """A Decision with a hand-picked value, everything else filled with harmless defaults, for exercising a
    generated class's `value()` at runtime without going through the engine."""
    return Decision(value=value, confidence=1.0, abstained=False, calibrated=False, interface="classify",
                    function=function_id, version="0.1.0", probabilities={}, score=None, route=(),
                    decided_by=function_id, escalations=(), timing={})


def _exec(source: str) -> dict[str, Any]:
    """Runs generated source and returns its namespace, so a test can call a generated class's `value()` directly.
    (The static type checks belong to mypy, exercised separately above and below; these tests are about the
    runtime TypeError, which Python itself enforces.)"""
    namespace: dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)
    return namespace


def test_score_value_rejects_bool_even_though_bool_equals_one() -> None:
    function = FunctionSpec(adapter_id="jeff.test.score", key="urgency", interface="rate", type="score",
                            instructions="How urgent?", labels=LabelSpec(source="fixed", levels=("low", "high")),
                            abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    cls = _exec(python_module(_catalogue_of(function), [function.id]))["TestScoreUrgency"]
    with pytest.raises(TypeError, match="outside the generated type"):
        cls.value(_decision(function.id, True))  # True == 1: must still be refused, it is not an int
    assert cls.value(_decision(function.id, 1)) == 1


def test_bool_value_rejects_int() -> None:
    function = FunctionSpec(adapter_id="jeff.test.noul", key="flag", interface="detect", type="noul",
                            instructions="Flag?", labels=LabelSpec(source="fixed"),
                            abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    cls = _exec(python_module(_catalogue_of(function), [function.id]))["TestNoulFlag"]
    with pytest.raises(TypeError, match="outside the generated type"):
        cls.value(_decision(function.id, 1))  # an int that happens to be 0 or 1 is still not a bool
    assert cls.value(_decision(function.id, True)) is True


def test_string_literal_rejects_int() -> None:
    function = FunctionSpec(adapter_id="jeff.test.fixed", key="kind", interface="classify", type="choice",
                            instructions="Which kind?", labels=LabelSpec(source="fixed", fixed={"a": None, "b": None}),
                            abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    cls = _exec(python_module(_catalogue_of(function), [function.id]))["TestFixedKind"]
    with pytest.raises(TypeError, match="outside the generated type"):
        cls.value(_decision(function.id, 1))
    assert cls.value(_decision(function.id, "a")) == "a"


def test_caller_str_rejects_int() -> None:
    function = FunctionSpec(adapter_id="jeff.test.caller", key="route", interface="route", type="choice",
                            instructions="Which team?", labels=LabelSpec(source="caller", max=10),
                            abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    cls = _exec(python_module(_catalogue_of(function), [function.id]))["TestCallerRoute"]
    with pytest.raises(TypeError, match="outside the generated type"):
        cls.value(_decision(function.id, 1))
    assert cls.value(_decision(function.id, "team-a")) == "team-a"


def test_requesting_a_function_via_its_adapter_and_itself_emits_it_once() -> None:
    source = python_module(Catalogue.from_dir(FIXTURES), ["spam", "spam.message_type"])
    assert source.count("class SafetySpamMessageType") == 1


def test_different_functions_colliding_on_class_name_raise() -> None:
    # "jeff.test.one-two.action" and "jeff.test.one.two_action" are different function IDs (different adapters,
    # different keys), but class_name() drops '-' and '_' the same way, so both become "TestOneTwoAction".
    one_two = FunctionSpec(adapter_id="jeff.test.one-two", key="action", interface="classify", type="choice",
                           instructions="?", labels=LabelSpec(source="fixed", fixed={"x": None}),
                           abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    one = FunctionSpec(adapter_id="jeff.test.one", key="two_action", interface="classify", type="choice",
                       instructions="?", labels=LabelSpec(source="fixed", fixed={"x": None}),
                       abstain_labels=(), calibration=None, context=(), input=MESSAGE)
    assert class_name(one_two.id) == class_name(one.id) == "TestOneTwoAction"
    catalogue = Catalogue([_manifest_with(one_two), _manifest_with(one)], bases={})
    with pytest.raises(JeffError) as excinfo:
        python_module(catalogue, [one_two.id, one.id])
    message = str(excinfo.value)
    assert one_two.id in message and one.id in message and "TestOneTwoAction" in message


def test_class_name_handles_a_part_starting_with_a_digit() -> None:
    name = class_name("jeff.3pl.routing.route")
    assert name == "N3plRoutingRoute"
    assert name.isidentifier()


def test_main_rejects_unsupported_lang(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["gen", "--lang", "go", "--out", str(tmp_path / "out.py"), "spam"])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "go" in captured.err and "jeff-sdk" in captured.err


def test_generated_module_for_every_shipped_adapter_compiles_and_passes_mypy_strict(tmp_path: Path) -> None:
    catalogue = Catalogue.shipped()
    refs = sorted(manifest.id for manifest in catalogue.manifests())
    source = python_module(catalogue, refs)
    compile(source, "functions.py", "exec")
    (tmp_path / "functions.py").write_text(source)
    result = subprocess.run([sys.executable, "-m", "mypy", "--strict", "functions.py"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
