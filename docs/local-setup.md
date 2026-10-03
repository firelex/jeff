# Local setup notes — jeff on the MacBook Air (M4)

Working notes from setting this up on 2 Oct 2026. **Measured on this machine** unless marked
*(estimate)*; figures quoted from the upstream README are marked as such.

- Machine: MacBook Air, Apple M4 (10 cores), macOS 26.6.2, fanless
- Model: `Jeff-Qwen3.5-0.8B-v1.2` + adapters `guard`, `tools`, `ground` (in `adapters/`)
- Also discussed: Intel NUC 10 (`NUC10i7FNH`, i7-10710U, 1× Thunderbolt 3, no PCIe x16) as a
  possible eGPU host

---

## 1. Why it was slow (fixed)

The README's quick-start line ends in a comment:

```bash
uv run --no-default-groups --extra lora jeff-serve       # add JEFF_BACKEND=mlx and --extra mac on Apple silicon
```

**zsh does not treat `#` as a comment inside an interactive shell by default** (`INTERACTIVE_COMMENTS`
is off), so pasting that line passes the whole comment through as arguments. Confirmed in `ps`:

```
uv run --no-default-groups --extra lora jeff-serve # add JEFF_BACKEND=mlx and --extra mac on Apple silicon
```

Consequences: `JEFF_BACKEND` stayed at its default `pytorch`, `--extra mac` was never installed
(MLX absent from `.venv`), and with no `JEFF_DEVICE` set the PyTorch path falls back to plain **CPU**
— not MPS (`src/jeff/models.py:31`, `src/jeff/model.py:145`).

| | per decision (112-token prompt) |
|---|---:|
| PyTorch on CPU (as launched by mistake) | **~3,000 ms** |
| MLX on the Apple GPU (correct) | **~54–63 ms** |

For reference: upstream README claims 28 ms on an M4 Max (MLX), 463 ms on a 32-thread CPU.

---

## 2. Launch / stop / verify

**Install (once — adds MLX):**

```bash
uv sync --no-default-groups --extra lora --extra mac
```

**Run** (nothing may follow `jeff-serve`; the comment must not be pasted):

```bash
JEFF_BACKEND=mlx JEFF_CHECKPOINT=Jeff-Qwen3.5-0.8B-v1.2 JEFF_ADAPTERS=adapters PORT=8765 \
  uv run --no-default-groups --extra lora --extra mac jeff-serve
```

**Stop:**

```bash
kill $(lsof -tiTCP:8765 -sTCP:LISTEN)   # or: pkill -f jeff-serve
lsof -nP -iTCP:8765 -sTCP:LISTEN || echo "port 8765 free"
```

**Verify the backend** — `http://127.0.0.1:8765/health`:

- `"modalities":["text"]` → MLX (correct)
- `"modalities":["text","image"]` → PyTorch (means it fell back)

Adapters present: `ground`, `guard`, `tools`. Extras on the CUDA path would be `--extra cuda`
(`flash-linear-attention`) + `JEFF_BACKEND=pytorch JEFF_DEVICE=cuda` — without it, transformers
falls back to much slower code.

---

## 3. Latency (measured, warm server, full HTTP round trip from localhost)

| Request | Input tokens | Median | Min–max |
|---|---:|---:|---:|
| 3-option question | 112 | **63 ms** | 57–70 |
| Mode classifier below | 213 | **109 ms** | 104–110 |
| Same, `state` ~10× longer | 390 | **185 ms** | 180–196 |
| Same, `"orders": 2` | 213×2 | **198 ms** | 198–199 |
| First request after startup | — | ~1.2 s | one-time warm-up |
| `guard` adapter request (`noul`) | — | **~55 ms** | 54–60 |

**Scaling model: ≈ 0.44 ms/token + ~15 ms fixed.** The forward pass is only a few ms — nearly all
of it is re-reading `instructions` + `criteria` on every call. Levers, in order:

1. **Shorten `criteria`** — free. ~100 tokens should land near **~60 ms** *(extrapolated)*.
2. **Don't use `orders: 2`** unless rows are borderline — it costs exactly 2× (and measured no
   accuracy gain on the test rows below).
3. **Batch independent questions** into one `questions` object instead of several requests.

### Concurrency

`jeff-serve` handles **one request at a time**. Measured: 4 simultaneous requests → one `200` in
70 ms, three `529` in ~4.5 ms with `Retry-After: 1`. Fine for a single agent loop; callers need a
retry, or a GPU/hosted backend that batches.

---

## 4. The mode classifier (`build` / `plan` / `terminal`)

`build`/`plan` = agent modes as in Codex / opencode; `terminal` = any shell command.

```bash
curl -s http://127.0.0.1:8765/v1/systemone \
  -H 'content-type: application/json' \
  -d '{
    "model": "jeff-latest",
    "state": "ls ~/dev",
    "questions": {
      "mode": {
        "type": "choice",
        "instructions": "Classify the action. Build and plan are agent modes, as in Codex or opencode. If the action executes a shell command, it is terminal.",
        "criteria": {
          "build":    "Build mode: the agent is making the changes - it edits or creates files, applies edits, and runs commands to carry the work out and verify it",
          "plan":     "Plan mode: the agent only looks and thinks - it reads, searches and inspects the codebase, then proposes a plan or an answer; it edits nothing and changes nothing",
          "terminal": "A shell command executed in a terminal: ls, cd, cat, grep, ps, git status, echo, rm, npm run build, uv sync"
        }
      }
    }
  }' | python3 -m json.tool
```

Results on held-out phrasings (orders=1):

| `state` | pick | build | plan | terminal |
|---|---|---:|---:|---:|
| `Read the repo and write a plan for adding a dark theme; change nothing` | plan | 0.27 | **0.72** | 0.01 |
| `Inspect src/jeff/server.py and explain how to add a --verbose flag, without editing` | plan | 0.14 | **0.75** | 0.10 |
| `Edit src/jeff/server.py to add a --verbose flag, then run the tests` | build | **0.83** | 0.08 | 0.08 |
| `apply the fix from the plan to src/jeff/models.py` | build | **0.86** | 0.11 | 0.03 |
| `ls ~/dev` | terminal | 0.14 | 0.10 | **0.76** |
| `npm run build` | terminal | 0.16 | 0.01 | **0.83** |

Two prompt notes:

- `plan` needed the explicit **"it edits nothing and changes nothing"** — before that, the dark-theme
  row scored 0.54 plan / 0.45 build (confidence 0.31). The `criteria` strings *are* the prompt; edit
  them, not the code, when a row lands wrong.
- Examples inside `criteria` set the boundaries: `npm run build` goes to `terminal` because it is
  listed there, while a sentence about editing goes to `build`.
- Keep option keys as short words (never bare numbers); put unchanging text in `state`.

---

## 5. Options for going faster

Measured network floor from this machine (relevant to every hosted option):

| | |
|---|---:|
| RTT → api.anthropic.com / huggingface.co | ~17 ms |
| RTT → github.com (far region) | ~80 ms |
| DNS + TCP + TLS to a nearby HTTPS endpoint | ~48 ms, **first call only** (keep-alive → 0) |

| Route | Cost | Warm latency, 213-token prompt |
|---|---:|---:|
| Trim `criteria` to ~100 tokens | **free** | ~60 ms *(extrapolated)* |
| Current: local MLX | — | 109 ms (measured) |
| NUC + used RTX 3060 12 GB in used TB3 enclosure | ~$350–500 | ~30–55 ms *(estimate)* |
| NUC + new RTX 4060 + new enclosure | ~$600–750 | ~25–45 ms *(estimate)* |
| Mac mini M4 (MLX, fan-cooled — the Air throttles under sustained load) | ~$599 | ~40–70 ms *(estimate)* |

**NUC constraints:** `NUC10i7FNH` has **no PCIe x16 slot** (M.2 only) but **1× Thunderbolt 3**, so a
GPU goes in an eGPU enclosure; documented builds exist (RTX 3070 + Razer Core X). TB3's 40 Gbps is
not a bottleneck here — weights (1.7 GB) load into VRAM once at startup, then only ~200 tokens cross
per request. **Do not run CPU inference on the NUC** — the 15 W i7-10710U would be ~0.5–1.5 s
*(estimate)*, worse than the Air's GPU.

> Parts-list check: a **288-pin desktop DDR4** stick (e.g. G.SKILL Aegis F4-2666C19D-32GIS) will not
> fit the NUC — it takes **260-pin SO-DIMMs**.

### Hosted / external

| Option | Latency from here | Calibrated probabilities | Notes |
|---|---:|---|---|
| **Jev** (TypeSafe) — drop-in | ~120–250 ms | yes (theirs) | $0.042/M input, $0 output ≈ $0.000009/call. Same schema: base URL `https://api.typesafe.ai`, model `jev-latest`; or OpenRouter `https://openrouter.ai/api/v1/systemone`. Jeff's adapters do **not** carry over |
| Hosted OSS model + logprobs | ~80–200 ms *(est.)* | weak — must re-calibrate | Groq / Together / Fireworks / DeepInfra; use single-token labels + `top_logprobs`, never JSON-elicited probabilities |
| Cloudflare Workers AI | ~30–100 ms *(est.)* | reranker scores only | $0.011/1k neurons, free 10k/day. `distilbert-sst-2-int8` $0.026/M (fixed sentiment); `bge-reranker-base` $0.003/M — score each label as a document, normalize to get probabilities |
| Hosted Jeff (Baseten / Modal / Replicate / HF) | ~50–150 ms *(est.)* | yes + your adapters | only route that keeps the trained readout; repo ships no Dockerfile, so the handler is yours |
| Big chat APIs (OpenAI/Anthropic) | 300 ms–2 s *(est.)* | no | overkill for a 3-way classification |

CloudFront was considered and ruled out: it is a CDN, not an inference service.

**Verdict:** nothing external beats local on raw latency — every hosted route adds the 17–80 ms
network floor on top of compute that already runs in 60–110 ms. External wins on **concurrency**
(no `529`), uptime and reachability. Order of attack: trim the prompt → if still not enough, eGPU or
Mac mini → if the constraint becomes scale/other clients, Jev as a base-URL swap.

---

## 6. Status

The server on port 8765 was started as a background process by the coding agent and **dies with that
session** — rerun the command in §2 to bring it back.
