"""Dispatch to a function whose fixed labels allow a caller subset (labels.subset true, as soc's action): the caller's
labels reach it exactly as on a direct call, so a dispatched call renders the same row a direct call with that subset
renders, and an invalid subset is refused the same way (InvalidLabels), locally and through a server (HTTP and the
channel). A fixed-label function without a subset still takes no caller labels (they are dropped, as before)."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import InvalidLabels
from jeff.testing import FakeEngine

from served import ROUTED, Mode, answers, jeff_server

ROUTERS = Path(__file__).resolve().parent / "fixtures" / "routers"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
SPAM = "jeff.safety.spam.message_type"
SUBSET = ["spam", "legitimate"]


def subset_routers(tmp_path: Path) -> Path:
    """The routing fixtures with spam.message_type's fixed labels allowing a caller subset."""
    folder = tmp_path / "routers"
    shutil.copytree(ROUTERS, folder)
    path = folder / "jeff.safety.spam" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["functions"]["message_type"]["labels"]["subset"] = True
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return folder


def leaf_rows(engine: FakeEngine) -> list[dict[str, Any]]:
    return [rows[0] for adapter, rows in engine.calls if adapter == "jeff.safety.spam"]


def test_local_dispatch_passes_a_subset_to_a_fixed_subset_leaf(tmp_path: Path) -> None:
    catalogue = Catalogue.from_dir(subset_routers(tmp_path))
    dispatched, direct = FakeEngine(answers, set(ROUTED)), FakeEngine(answers, set(ROUTED))
    decision = Jeff(dispatched, catalogue).classify("WIN NOW", context=CONTEXT, labels=SUBSET)
    Jeff(direct, catalogue).classify("WIN NOW", function=SPAM, context={"channel": "email"}, labels=SUBSET)
    assert decision.function == SPAM and set(decision.probabilities) == set(SUBSET)
    assert leaf_rows(dispatched) == leaf_rows(direct)
    assert list(leaf_rows(dispatched)[0]["question"]["criteria"]) == ["legitimate", "spam"]  # in manifest order


def test_local_dispatch_refuses_an_invalid_subset_as_a_direct_call_does(tmp_path: Path) -> None:
    catalogue = Catalogue.from_dir(subset_routers(tmp_path))
    with pytest.raises(InvalidLabels) as direct:
        Jeff(FakeEngine(answers, set(ROUTED)), catalogue).classify("WIN NOW", function=SPAM,
                                                                   context={"channel": "email"}, labels=["spam", "ham"])
    engine = FakeEngine(answers, set(ROUTED))
    with pytest.raises(InvalidLabels) as dispatched:
        Jeff(engine, catalogue).classify("WIN NOW", context=CONTEXT, labels=["spam", "ham"])
    assert str(dispatched.value) == str(direct.value)
    assert leaf_rows(engine) == []


def test_local_dispatch_without_labels_offers_the_whole_fixed_set(tmp_path: Path) -> None:
    engine = FakeEngine(answers, set(ROUTED))
    Jeff(engine, Catalogue.from_dir(subset_routers(tmp_path))).classify("WIN NOW", context=CONTEXT)
    assert list(leaf_rows(engine)[0]["question"]["criteria"]) == ["legitimate", "spam", "phishing"]


def test_remote_dispatch_passes_a_subset_and_refuses_an_invalid_one(tmp_path: Path, mode: Mode) -> None:
    folder = subset_routers(tmp_path)
    direct = FakeEngine(answers, set(ROUTED))
    Jeff(direct, Catalogue.from_dir(folder)).classify("WIN NOW", function=SPAM, context={"channel": "email"},
                                                      labels=SUBSET)
    engine = FakeEngine(answers, set(ROUTED))
    with jeff_server(engine, folder) as url, mode.remote(url, catalogue=folder) as client:
        decision = client.classify("WIN NOW", context=CONTEXT, labels=SUBSET)
        assert decision.function == SPAM and set(decision.probabilities) == set(SUBSET)
        assert leaf_rows(engine) == leaf_rows(direct)
        with pytest.raises(InvalidLabels, match="ham"):
            client.classify("WIN NOW", context=CONTEXT, labels=["spam", "ham"])
