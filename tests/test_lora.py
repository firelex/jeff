"""LoRA adapters (jeff.lora): training, saving, loading, and several adapters on one base in the server. Tiny random
models on the CPU, as in test_decoder.py."""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import torch
from fastapi.testclient import TestClient
from safetensors.torch import load_file
from transformers import AutoTokenizer, Gemma3ForCausalLM, Gemma3TextConfig
from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5Config
from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5Model

from jeff import lora, server, train
from jeff.decoder import DECODER_MODELS, GenericDecoderDecisionModel
from jeff.models import load_decision_model
from jeff.optim import CPUOffloadAdamW

BASE = "google/gemma-3-270m-it"
REVISION = DECODER_MODELS[BASE][0]
ROWS: list[Any] = [
    {"state": "The parcel never arrived.", "question": {"type": "choice", "instructions": "Route the message.",
                                                        "criteria": {"billing": "Charges.", "delivery": "Shipping.", "other": None}}},
    {"state": "The sky is green.", "question": {"type": "noul", "instructions": "Is the statement true?"}},
]


@pytest.fixture(scope="module")
def tiny_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A full Jeff checkpoint of a tiny random Gemma 3 text model with the real Gemma tokenizer (CPU, seconds)."""
    path = tmp_path_factory.mktemp("tiny-gemma")
    tokenizer = AutoTokenizer.from_pretrained(BASE, revision=REVISION)
    config = Gemma3TextConfig(vocab_size=len(tokenizer), hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                              num_attention_heads=2, num_key_value_heads=1, head_dim=16, pad_token_id=tokenizer.pad_token_id)
    config.architectures = ["Gemma3ForCausalLM"]
    torch.manual_seed(0)
    Gemma3ForCausalLM(config).save_pretrained(path / "hf")
    tokenizer.save_pretrained(path / "hf")
    model = GenericDecoderDecisionModel(base_model=str(path / "hf"), revision=REVISION, device="cpu")
    model.save(path / "jeff", temperature=1.1, max_options=26)
    return path / "jeff"


def adapter(base: Path, destination: Path, seed: int, rank: int = 4, max_options: int = 26) -> Path:
    """Train-free adapter: attach, give the LoRA weights and the readout random nonzero values, save."""
    model = load_decision_model(checkpoint=base, device="cpu", train=True)
    lora.attach(model, lora.LoraSettings(rank, 2 * rank), base)
    generator = torch.Generator().manual_seed(seed)
    with torch.no_grad():
        for name, parameter in model.named_parameters():
            if "lora_" in name or name.startswith("readout."):
                parameter.copy_(torch.randn(parameter.shape, generator=generator) * 0.2)
    model.eval()
    lora.save_adapter(model, destination, temperature=0.5 + seed / 4, max_options=max_options, step=1)
    return destination


def probabilities(model: torch.nn.Module) -> list[list[float]]:
    return model.predict(ROWS, temperature=1.0)  # type: ignore[operator, no-any-return]


def close(a: list[list[float]], b: list[list[float]]) -> bool:
    return all(abs(x - y) < 1e-5 for pa, pb in zip(a, b, strict=True) for x, y in zip(pa, pb, strict=True))


def test_targets_are_the_text_decoders_attention_and_mlp_projections(tiny_base: Path) -> None:
    gemma = load_decision_model(checkpoint=tiny_base, device="cpu")
    names = lora.target_modules(gemma.backbone)  # type: ignore[arg-type]
    assert len(names) == 2 * 7 and {name.rsplit(".", 1)[1] for name in names} == {
        "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}
    config = Qwen3_5Config(
        text_config={"hidden_size": 32, "intermediate_size": 64, "num_hidden_layers": 4, "num_attention_heads": 2,
                     "num_key_value_heads": 1, "head_dim": 16, "vocab_size": 1000, "linear_num_key_heads": 2,
                     "linear_num_value_heads": 2, "linear_key_head_dim": 16, "linear_value_head_dim": 16},
        vision_config={"depth": 1, "hidden_size": 32, "intermediate_size": 64, "num_heads": 2, "out_hidden_size": 32})
    qwen = lora.target_modules(Qwen3_5Model(config))
    kinds = {name.rsplit(".", 1)[1] for name in qwen}
    assert kinds == {"in_proj_qkv", "in_proj_z", "out_proj", "q_proj", "k_proj", "v_proj", "o_proj",
                     "gate_proj", "up_proj", "down_proj"}
    assert all(name.startswith("language_model.") for name in qwen)  # never the vision tower
    assert len(qwen) == 3 * 6 + 7  # three linear-attention layers and one full-attention layer
    with pytest.raises(ValueError, match="no attention or MLP projections"):
        lora.target_modules(torch.nn.Sequential(torch.nn.Linear(2, 2)))


def test_attach_freezes_the_backbone_and_trains_lora_and_readout(tiny_base: Path) -> None:
    model = load_decision_model(checkpoint=tiny_base, device="cpu", train=True)
    before = probabilities(model)
    lora.attach(model, lora.LoraSettings(4, 8), tiny_base)
    trainable = [name for name, parameter in model.named_parameters() if parameter.requires_grad]
    assert trainable and all("lora_" in name or name == "readout.weight" for name in trainable)
    assert close(probabilities(model), before)  # LoRA B starts at zero: the model is unchanged until trained
    with pytest.raises(ValueError, match="save a trained adapter with jeff.lora.save_adapter"):
        model.save(tiny_base.parent / "never")  # type: ignore[operator]


def test_adapter_folder_round_trip(tiny_base: Path, tmp_path: Path) -> None:
    folder = adapter(tiny_base, tmp_path / "nav", seed=1)
    assert sorted(path.name for path in folder.iterdir()) == [
        "README.md", "adapter_config.json", "adapter_model.safetensors", "decision_config.json", "readout.safetensors"]
    config = json.loads((folder / "decision_config.json").read_text())
    assert config["adapter"]["rank"] == 4 and config["adapter"]["alpha"] == 8
    assert config["adapter"]["base_checkpoint"] == {"path": str(tiny_base.resolve()),
                                                    "weights_sha256": lora.weights_sha256(tiny_base)}
    assert config["architecture"] == "decoder-generic" and config["max_options"] == 26 and config["temperature"] == 0.75
    weights = load_file(str(folder / "adapter_model.safetensors"))
    assert len(weights) == 2 * 14 and all(".lora_A." in key or ".lora_B." in key for key in weights)
    loaded = load_decision_model(checkpoint=folder, device="cpu")
    assert loaded.temperature == 0.75
    base = load_decision_model(checkpoint=tiny_base, device="cpu")
    assert not close(probabilities(loaded), probabilities(base))
    with pytest.raises(ValueError, match="Training on top of an adapter"):
        load_decision_model(checkpoint=folder, device="cpu", train=True)


def test_one_base_several_adapters_switch_without_reloading(tiny_base: Path, tmp_path: Path) -> None:
    folders = {"nav": adapter(tiny_base, tmp_path / "nav", seed=1), "tools": adapter(tiny_base, tmp_path / "tools", seed=2)}
    expected = {name: probabilities(load_decision_model(checkpoint=path, device="cpu")) for name, path in folders.items()}
    model = load_decision_model(checkpoint=tiny_base, device="cpu")
    plain = probabilities(model)
    backbone_weights = {name: parameter for name, parameter in model.backbone.named_parameters()}
    adapters = lora.AdapterSet(model, tiny_base, folders, precision="float32")
    assert adapters.names == ["nav", "tools"]
    for name in ["nav", "tools", None, "tools", "nav", None, "nav"]:  # interleaved requests
        adapters.use(name)
        assert close(probabilities(model), plain if name is None else expected[name])
        assert model.temperature == (1.1 if name is None else json.loads((folders[name] / "decision_config.json").read_text())["temperature"])
    # the base weights were never copied or moved
    shared = {name.replace(".base.", "."): parameter for name, parameter in model.backbone.named_parameters()}
    assert shared.keys() == backbone_weights.keys()
    assert all(shared[name] is parameter for name, parameter in backbone_weights.items())
    with pytest.raises(ValueError, match="Unknown adapter"):
        adapters.use("guard")
    with pytest.raises(ValueError, match="LoRA precision"):
        lora.AdapterSet(load_decision_model(checkpoint=tiny_base, device="cpu"), tiny_base, precision="bf16")


def peft_reference(base: Path, folder: Path) -> list[list[float]]:
    """The same adapter applied by PEFT itself (as trained), for comparison with the serving layers."""
    from peft import PeftModel

    model = load_decision_model(checkpoint=base, device="cpu")
    model.backbone = PeftModel.from_pretrained(model.backbone, str(folder), is_trainable=False)  # type: ignore[assignment]
    config = json.loads((folder / "decision_config.json").read_text())
    model.readout.load_state_dict(load_file(str(folder / "readout.safetensors")))  # type: ignore[operator]
    model.eval()
    assert config["temperature"] > 0
    return probabilities(model)


def test_serving_layers_match_peft_in_both_precisions(tiny_base: Path, tmp_path: Path) -> None:
    folder = adapter(tiny_base, tmp_path / "nav", seed=1)
    reference = peft_reference(tiny_base, folder)
    for precision in lora.PRECISIONS:  # on the CPU the model's dtype is float32, so both must match PEFT closely
        model = load_decision_model(checkpoint=tiny_base, device="cpu")
        lora.AdapterSet(model, tiny_base, {"nav": folder}, precision=precision).use("nav")
        assert close(probabilities(model), reference), precision


def test_bfloat16_updates_stay_close_to_float32() -> None:
    torch.manual_seed(0)
    base = torch.nn.Linear(64, 48, bias=False)
    a, b = torch.randn(8, 64) * 0.1, torch.randn(48, 8) * 0.1
    x = torch.randn(3, 5, 64)
    reference = lora.LoraLinear(base, "float32")
    reference.add("t", a, b, 2.0)
    reference.active = "t"
    expected = base(x) + 2.0 * (x @ a.t()) @ b.t()
    assert torch.allclose(reference(x), expected, atol=1e-5)
    half = lora.LoraLinear(torch.nn.Linear(64, 48, bias=False, dtype=torch.bfloat16), "model")
    half.base.weight.data.copy_(base.weight.data.to(torch.bfloat16))
    half.add("t", a, b, 2.0)
    half.active = "t"
    assert half.updates["t"][0].dtype == torch.bfloat16
    out = half(x.to(torch.bfloat16))
    assert out.dtype == torch.bfloat16 and torch.allclose(out.float(), expected, atol=0.05, rtol=0.02)
    half.active = None
    assert torch.equal(half(x.to(torch.bfloat16)), half.base(x.to(torch.bfloat16)))
    with pytest.raises(ValueError, match="do not fit"):
        half.add("u", torch.randn(8, 63), b, 2.0)


def test_merged_mode_runs_one_adapter_inside_the_weights(tiny_base: Path, tmp_path: Path) -> None:
    folder = adapter(tiny_base, tmp_path / "nav", seed=1)
    shared = load_decision_model(checkpoint=tiny_base, device="cpu")
    lora.AdapterSet(shared, tiny_base, {"nav": folder}, precision="float32").use("nav")
    model = load_decision_model(checkpoint=tiny_base, device="cpu")
    head = lora.merge_adapter(model, tiny_base, "nav", folder)
    assert model.merged_adapter == "nav" and model.temperature == head.temperature == 0.75
    assert not any(isinstance(module, lora.LoraLinear) for module in model.modules())
    assert close(probabilities(model), probabilities(shared))
    with pytest.raises(ValueError, match="already merged"):
        lora.merge_adapter(model, tiny_base, "nav", folder)
    with pytest.raises(ValueError, match="merged into its weights"):
        lora.AdapterSet(model, tiny_base, precision="model")
    with pytest.raises(ValueError, match="LoRA adapters"):
        model.save(tmp_path / "never")  # type: ignore[operator]
    with pytest.raises(ValueError, match="shared-base mode"):
        lora.merge_adapter(shared, tiny_base, "nav", folder)


def test_an_adapter_refuses_a_different_base(tiny_base: Path, tmp_path: Path) -> None:
    folder = adapter(tiny_base, tmp_path / "nav", seed=1)
    other = tmp_path / "other-base"
    model = load_decision_model(checkpoint=tiny_base, device="cpu")
    with torch.no_grad():
        next(model.backbone.parameters()).add_(1.0)
    model.save(other, max_options=26)  # type: ignore[operator]
    with pytest.raises(ValueError, match="weights differ"):
        lora.AdapterSet(load_decision_model(checkpoint=other, device="cpu"), other, {"nav": folder}, precision="model")
    with pytest.raises(ValueError, match="weights differ"):
        lora.merge_adapter(load_decision_model(checkpoint=other, device="cpu"), other, "nav", folder)
    with pytest.raises(ValueError, match="Adapter name"):
        lora.AdapterSet(load_decision_model(checkpoint=tiny_base, device="cpu"), tiny_base, {"Nav": folder}, precision="model")


def test_optimizer_learning_rates_per_parameter() -> None:
    first, second = torch.nn.Parameter(torch.ones(2)), torch.nn.Parameter(torch.ones(3))
    single = CPUOffloadAdamW([("a", first), ("readout.weight", second)], lr=1e-3)
    assert [(group["lr"], group["peak_lr"], len(group["params"])) for group in single.param_groups] == [(1e-3, 1e-3, 2)]
    split = CPUOffloadAdamW([("a", first), ("readout.weight", second)], lr=1e-3, learning_rates={"readout.weight": 1e-5})
    assert [(group["peak_lr"], len(group["params"])) for group in split.param_groups] == [(1e-3, 1), (1e-5, 1)]


def request(model: str, count: int = 3) -> dict[str, Any]:
    return {"model": model, "state": "The parcel never arrived.",
            "questions": {"q": {"type": "choice", "instructions": "Route the message.",
                                "criteria": dict(list({"billing": "Charges.", "delivery": "Shipping.", "other": None,
                                                       "refund": "Money back."}.items())[:count])},
                          "n": {"type": "noul", "instructions": "Is the customer angry?"}}}


def test_server_chooses_the_adapter_per_request(tiny_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter(tiny_base, tmp_path / "adapters" / "nav", seed=1)
    adapter(tiny_base, tmp_path / "adapters" / "tools", seed=2, max_options=3)
    monkeypatch.setenv("JEFF_CHECKPOINT", str(tiny_base))
    monkeypatch.setenv("JEFF_ADAPTERS", str(tmp_path / "adapters"))
    monkeypatch.setenv("JEFF_DEVICE", "cpu")
    monkeypatch.delenv("JEFF_API_KEY", raising=False)
    with TestClient(server.app) as client:
        health = client.get("/health").json()
        assert health["adapters"] == {"nav": {"max_options": 26}, "tools": {"max_options": 3}}
        assert {entry["name"] for entry in client.get("/v1/models").json()["models"]} >= {"jeff", "nav", "tools"}
        answers: dict[str, list[dict[str, Any]]] = {}
        for name in ["nav", "jeff", "tools", "nav", "tools", "jeff"]:
            response = client.post("/v1/systemone", json=request(name))
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["model"] == (server.service.name if name == "jeff" else name)
            answers.setdefault(name, []).append(body["answers"])
        assert all(values[0] == values[1] for values in answers.values())  # switching back gives the same answers
        assert answers["nav"][0] != answers["tools"][0] != answers["jeff"][0]
        refused = client.post("/v1/systemone", json=request("tools", count=4))
        assert refused.status_code == 422 and "at most 3" in refused.text
        assert client.post("/v1/systemone", json=request("nav", count=4)).status_code == 200
        unknown = client.post("/v1/systemone", json=request("guard"))
        assert unknown.status_code == 422 and "nav, tools" in unknown.text
    direct = load_decision_model(checkpoint=tmp_path / "adapters" / "nav", device="cpu")
    [expected] = direct.predict([{"state": "The parcel never arrived.", "question": request("nav")["questions"]["q"]}])  # type: ignore[operator]
    assert all(abs(answers["nav"][0]["q"]["probabilities"][key] - value) < 1e-5
               for key, value in zip(["billing", "delivery", "other"], expected))


def test_answer_twice_uses_the_requested_adapter_for_both_orders(tiny_base: Path, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """orders=2 on an adapter: both the given and the reversed option order are read with that adapter."""
    from jeff.orders import predict_orders

    adapter(tiny_base, tmp_path / "adapters" / "nav", seed=1)
    monkeypatch.setenv("JEFF_CHECKPOINT", str(tiny_base))
    monkeypatch.setenv("JEFF_ADAPTERS", str(tmp_path / "adapters"))
    monkeypatch.setenv("JEFF_DEVICE", "cpu")
    monkeypatch.delenv("JEFF_API_KEY", raising=False)
    with TestClient(server.app) as client:
        client.post("/v1/systemone", json=request("jeff"))  # leave the base active before the adapter request
        response = client.post("/v1/systemone", json=request("nav") | {"orders": 2})
        assert response.status_code == 200, response.text
        body = response.json()
    assert body["model"] == "nav" and body["usage"]["orders"] == 2
    direct = load_decision_model(checkpoint=tmp_path / "adapters" / "nav", device="cpu")
    [expected] = predict_orders(direct, [{"state": "The parcel never arrived.", "question": request("nav")["questions"]["q"]}], 2)  # type: ignore[arg-type]
    assert all(abs(body["answers"]["q"]["probabilities"][key] - value) < 1e-5
               for key, value in zip(["billing", "delivery", "other"], expected))


def training_rows(path: Path, family: str, count: int) -> Path:
    labels = ["billing", "delivery", "other"]
    with path.open("w") as stream:
        for index in range(count):
            row = {"id": f"{family}-{index}", "suite": "tiny", "family": family, "source": {},
                   "state": f"Message {index}: {labels[index % 3]} problem.",
                   "question": {"type": "choice", "instructions": "Route the message.",
                                "criteria": {"billing": "Charges.", "delivery": "Shipping.", "other": None}},
                   "label": labels[index % 3], "target": labels[index % 3]}
            stream.write(json.dumps(row) + "\n")
    return path


@pytest.mark.parametrize("mode", ["lora", "full"])
def test_jeff_train_end_to_end(mode: str, tiny_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """jeff-train on the CPU (its CUDA-only timing calls stubbed): --lora-rank writes adapter folders; without it the
    full-weight run writes full checkpoints as before."""
    monkeypatch.setattr(torch.cuda, "synchronize", lambda *args: None)
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", lambda *args: 0)
    monkeypatch.setenv("JEFF_EVENTS", str(tmp_path / "events.jsonl"))
    config = json.loads((tiny_base / "decision_config.json").read_text())
    extra = ["--lora-rank", "4", "--lr", "1e-2", "--readout-lr", "1e-4"] if mode == "lora" else ["--lr", "1e-4"]
    monkeypatch.setattr(sys, "argv", [
        "jeff-train", "--train", str(training_rows(tmp_path / "train.jsonl", "train", 8)),
        "--development", str(training_rows(tmp_path / "dev.jsonl", "dev", 3)),
        "--temperature", str(training_rows(tmp_path / "calibration.jsonl", "calibration", 3)),
        "--run", str(tmp_path / "run"), "--output", str(tmp_path / "out"), "--base-model", config["base_model"],
        "--revision", REVISION, "--initial-checkpoint", str(tiny_base), "--epochs", "1", "--batch-size", "4",
        "--effective-batch-size", "4", "--eval-every", "1", "--cpu-threads", "2", *extra])
    train.main()
    final = tmp_path / "out" / "final"
    files = {path.name for path in final.iterdir()}
    summary = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert summary["complete"] and summary["steps"] == 2
    if mode == "full":
        assert "model.safetensors" in files and "adapter_model.safetensors" not in files
        return
    assert "adapter_model.safetensors" in files and "model.safetensors" not in files
    saved = json.loads((final / "decision_config.json").read_text())
    assert saved["adapter"]["rank"] == 4 and saved["adapter"]["alpha"] == 8 and saved["max_options"] == 3
    weights = load_file(str(final / "adapter_model.safetensors"))
    assert any(".lora_B." in key and value.abs().sum() > 0 for key, value in weights.items())  # the adapter trained
    readout = load_file(str(final / "readout.safetensors"))["weight"]
    base_readout = load_file(str(tiny_base / "readout.safetensors"))["weight"]
    assert 0 < (readout - base_readout).abs().max() < 1e-3  # the readout trained at its own, small rate
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    started = next(event for event in events if event["kind"] == "training_started")
    assert started["trainable_parameters"] == sum(value.numel() for value in weights.values()) + readout.numel()
    load_decision_model(checkpoint=final, device="cpu")  # the saved adapter loads


def test_mlx_multi_adapter_layers_match_peft(tmp_path: Path) -> None:
    """The MLX backend's adapter layers give the same hidden states as PEFT in PyTorch, for each adapter and for the
    base (a tiny random Qwen3.5; no tokenizer needed)."""
    mx = pytest.importorskip("mlx.core")  # Apple silicon only
    from mlx_lm.models.qwen3_5 import Model, ModelArgs
    from peft import LoraConfig, PeftModel, get_peft_model

    from jeff.mlx_lora import add_adapter, add_adapters, remove_adapter

    config = Qwen3_5Config(
        text_config={"hidden_size": 32, "intermediate_size": 64, "num_hidden_layers": 4, "num_attention_heads": 2,
                     "num_key_value_heads": 1, "head_dim": 16, "vocab_size": 1000, "linear_num_key_heads": 2,
                     "linear_num_value_heads": 2, "linear_key_head_dim": 32, "linear_value_head_dim": 32,  # MLX's kernel needs 32+
                     "tie_word_embeddings": True},  # as the real 0.8B and 2B: MLX then expects no separate lm_head
        vision_config={"depth": 1, "hidden_size": 32, "intermediate_size": 64, "num_heads": 2, "out_hidden_size": 32},
        tie_word_embeddings=True)
    torch.manual_seed(0)
    backbone = Qwen3_5Model(config).eval()
    backbone.save_pretrained(tmp_path / "base")
    tokens = [[5, 17, 300, 42, 7, 999, 3]]
    folders: dict[str, Path] = {}
    for seed, name in enumerate(["nav", "tools"]):
        wrapped = get_peft_model(Qwen3_5Model.from_pretrained(tmp_path / "base"),
                                 LoraConfig(r=4, lora_alpha=8, target_modules=lora.target_modules(backbone)))
        generator = torch.Generator().manual_seed(seed)
        with torch.no_grad():
            for parameter_name, parameter in wrapped.named_parameters():
                if "lora_" in parameter_name:
                    parameter.copy_(torch.randn(parameter.shape, generator=generator) * 0.2)
        wrapped.save_pretrained(tmp_path / name)
        folders[name] = tmp_path / name
    served = PeftModel.from_pretrained(Qwen3_5Model.from_pretrained(tmp_path / "base"), str(folders["nav"]), adapter_name="nav")
    served.load_adapter(str(folders["tools"]), adapter_name="tools")
    served.eval()
    expected: dict[str | None, torch.Tensor] = {}
    with torch.no_grad():
        for name in ("nav", "tools"):
            served.set_adapter(name, inference_mode=True)
            expected[name] = served(input_ids=torch.tensor(tokens)).last_hidden_state[0, -1]
        with served.disable_adapter():
            expected[None] = served(input_ids=torch.tensor(tokens)).last_hidden_state[0, -1]
    model = Model(ModelArgs.from_dict(json.loads((tmp_path / "base" / "config.json").read_text())))
    raw = mx.load(str(tmp_path / "base" / "model.safetensors"))
    renamed = {f"model.{key}": value for key, value in raw.items() if key.startswith("language_model.")}
    model.load_weights(list(model.sanitize(renamed).items()), strict=True)
    layers = add_adapters(model, folders, "float32")
    model.eval()
    assert len(layers) == len(lora.target_modules(backbone))
    for name in ["nav", None, "tools", "nav"]:
        for layer in layers:
            layer.active = name
        hidden = model.language_model.model(mx.array(tokens))[0, -1]
        differences = {key: float((torch.tensor(hidden.tolist()) - value).abs().max()) for key, value in expected.items()}
        # MLX and PyTorch differ by about 0.006 on the plain base already; the adapters differ from each other by ~3
        assert differences[name] < 0.05 and all(value > 1 for key, value in differences.items() if key != name), (name, differences)
    remove_adapter(model, "nav")
    assert all("nav" not in layer.adapters and layer.active is None for layer in layers)
    layers = add_adapter(model, "nav", folders["nav"], "float32")  # added again: reuses the existing adapter layers
    assert len(layers) == len(lora.target_modules(backbone))
    for layer in layers:
        layer.active = "nav"
    hidden = model.language_model.model(mx.array(tokens))[0, -1]
    assert float((torch.tensor(hidden.tolist()) - expected["nav"]).abs().max()) < 0.05


def test_adapters_are_added_and_removed_while_serving(tiny_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "adapters"
    folder.mkdir()  # the server may start with no adapters at all
    monkeypatch.setenv("JEFF_CHECKPOINT", str(tiny_base))
    monkeypatch.setenv("JEFF_ADAPTERS", str(folder))
    monkeypatch.setenv("JEFF_DEVICE", "cpu")
    monkeypatch.delenv("JEFF_API_KEY", raising=False)
    with TestClient(server.app) as client:
        base = client.post("/v1/systemone", json=request("jeff")).json()["answers"]
        assert client.post("/v1/systemone", json=request("nav")).status_code == 422
        adapter(tiny_base, folder / "nav", seed=1)
        adapter(tiny_base, folder / "tools", seed=2)
        assert client.post("/v1/adapters/reload").json() == {
            "added": ["nav", "tools"], "removed": [], "reloaded": [], "adapters": ["nav", "tools"]}
        nav = client.post("/v1/systemone", json=request("nav")).json()["answers"]
        assert nav != base
        shutil.rmtree(folder / "nav")
        adapter(tiny_base, folder / "nav", seed=3)  # retrained: new weights in the same folder
        shutil.rmtree(folder / "tools")
        adapter(tiny_base, folder / "guard", seed=4)
        assert client.post("/v1/adapters/reload").json() == {
            "added": ["guard"], "removed": ["tools"], "reloaded": ["nav"], "adapters": ["guard", "nav"]}
        assert client.post("/v1/systemone", json=request("nav")).json()["answers"] not in (nav, base)
        assert client.post("/v1/systemone", json=request("tools")).status_code == 422
        assert client.post("/v1/systemone", json=request("guard")).status_code == 200
        assert client.post("/v1/systemone", json=request("jeff")).json()["answers"] == base
        shutil.rmtree(folder / "nav")
        shutil.rmtree(folder / "guard")
        assert client.post("/v1/adapters/reload").json()["adapters"] == []
        assert client.post("/v1/systemone", json=request("jeff")).json()["answers"] == base


def test_reload_is_refused_without_adapters(tiny_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEFF_CHECKPOINT", str(tiny_base))
    monkeypatch.delenv("JEFF_ADAPTERS", raising=False)
    monkeypatch.setenv("JEFF_DEVICE", "cpu")
    monkeypatch.delenv("JEFF_API_KEY", raising=False)
    with TestClient(server.app) as client:
        response = client.post("/v1/adapters/reload")
        assert response.status_code == 409 and "JEFF_ADAPTERS" in response.text
        assert client.get("/health").json()["adapters"] == {}


def serve_env(monkeypatch: pytest.MonkeyPatch, base: Path, adapters: Path, **extra: str) -> None:
    monkeypatch.setenv("JEFF_CHECKPOINT", str(base))
    monkeypatch.setenv("JEFF_ADAPTERS", str(adapters))
    monkeypatch.setenv("JEFF_DEVICE", "cpu")
    monkeypatch.delenv("JEFF_API_KEY", raising=False)
    for key in ("JEFF_ADAPTER_MODE", "JEFF_LORA_PRECISION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_server_merged_mode_serves_only_its_adapter(tiny_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter(tiny_base, tmp_path / "one" / "nav", seed=1)
    serve_env(monkeypatch, tiny_base, tmp_path / "one")
    with TestClient(server.app) as client:
        shared = client.post("/v1/systemone", json=request("nav")).json()["answers"]
    serve_env(monkeypatch, tiny_base, tmp_path / "one", JEFF_ADAPTER_MODE="merged")
    with TestClient(server.app) as client:
        assert client.get("/health").json()["merged_adapter"] == "nav"
        assert [entry["name"] for entry in client.get("/v1/models").json()["models"]] == ["nav"]
        merged = client.post("/v1/systemone", json=request("nav")).json()
        assert merged["model"] == "nav"
        for key, answer in shared.items():
            for option, value in answer.get("probabilities", {}).items():
                assert abs(merged["answers"][key]["probabilities"][option] - value) < 1e-4
        refused = client.post("/v1/systemone", json=request("jeff"))
        assert refused.status_code == 422 and "serves only nav" in refused.text
        assert client.post("/v1/adapters/reload").status_code == 409


def test_server_refuses_wrong_adapter_settings(tiny_base: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter(tiny_base, tmp_path / "two" / "nav", seed=1)
    adapter(tiny_base, tmp_path / "two" / "tools", seed=2)
    for extra, message in [({"JEFF_ADAPTER_MODE": "merged"}, "exactly one adapter"),
                           ({"JEFF_ADAPTER_MODE": "fast"}, "JEFF_ADAPTER_MODE"),
                           ({"JEFF_LORA_PRECISION": "half"}, "JEFF_LORA_PRECISION")]:
        serve_env(monkeypatch, tiny_base, tmp_path / "two", **extra)
        with pytest.raises(ValueError, match=message):
            with TestClient(server.app):
                pass
