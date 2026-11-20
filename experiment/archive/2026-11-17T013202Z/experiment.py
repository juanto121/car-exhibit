"""Base-prompt experiment: incidental pulmonary nodule.

All model calls (the two models under test and the judge) go through OpenRouter,
each pinned to its maker's own endpoint with provider fallbacks turned off.

Sends the same basic prompt N times to each model, saves every raw response,
and scores each report with a fixed, rule-based rubric for:

  - Organization  (required sections, in order)
  - Completeness  (no omitted findings or descriptors)
  - Fidelity      (no fabricated or altered content)

The rubric is defined in this file (see RUBRIC below) and documented in RUBRIC.md.
Generation and scoring are separate steps: scoring reads only the saved raw files,
so anyone can re-score the exact same outputs and get the exact same numbers.

Usage (from this folder):
  .venv/bin/python experiment.py estimate            # cost estimate, no API calls
  .venv/bin/python experiment.py run --n 3           # pilot: 3 calls per model
  .venv/bin/python experiment.py cost                # actual cost so far + projection to N
  .venv/bin/python experiment.py run                 # full run (resumes; skips finished calls)
  .venv/bin/python experiment.py judge               # model-as-judge on every saved report (resumes)
  .venv/bin/python experiment.py score               # regex + judge scores, summary (no API calls)
  .venv/bin/python experiment.py review              # sample of reports for a radiologist to check
  .venv/bin/python experiment.py judge --judge-pass 2        # re-judge 20 reports per model (later)
  .venv/bin/python experiment.py consistency --judge-pass 2  # pass 1 vs pass 2 agreement
  .venv/bin/python experiment.py archive             # dated read-only snapshot (also done by `score`)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "results" / "raw"
OUT_DIR = HERE / "results"

# ---------------------------------------------------------------------------
# Experiment definition (frozen). Change these only by starting a new results folder.
# ---------------------------------------------------------------------------

PROMPT = (
    "70-year-old man, never smoked. Chest CT for chronic cough. Single 7 mm solid nodule, "
    "right lower lobe, smooth margins. No prior CT. Write the report."
)
PROMPT_SHA256 = hashlib.sha256(PROMPT.encode()).hexdigest()

N_DEFAULT = 100
SEED_BASE = 2027
MAX_OUTPUT_TOKENS = 16000

# All calls go through OpenRouter (one API key, one API). Each model is pinned to its
# maker's own endpoint with fallbacks off, so every call is served by the same provider.
# IDs and prices can be overridden in .env; whatever is used is logged per call.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MODELS = {
    "openai": {
        "label": "GPT-6.1 Sol",
        "model_env": "OPENAI_MODEL", "model_default": "openai/gpt-6.1-sol",
        "effort_env": "OPENAI_EFFORT", "effort_default": "medium",
        "route_env": "OPENAI_ROUTE", "route_default": "openai",   # OpenRouter provider slug
        "supports_seed": True,
        # USD per 1M tokens, OpenRouter batch price (sync is 2x). Reasoning tokens billed as output.
        "price_in": 1.00, "price_out": 5.00,
    },
    "anthropic": {
        "label": "Claude Opus 5.5",
        "model_env": "ANTHROPIC_MODEL", "model_default": "anthropic/claude-opus-5.5",
        "effort_env": "ANTHROPIC_EFFORT", "effort_default": "medium",
        "route_env": "ANTHROPIC_ROUTE", "route_default": "anthropic",
        "supports_seed": False,
        # USD per 1M tokens, batch price (sync is 2x). Thinking tokens billed as output.
        "price_in": 2.00, "price_out": 10.00,
    },
}

# Assumptions for the pre-run estimate (replaced by measured usage after a pilot).
EST_INPUT_TOKENS = 60
EST_OUTPUT_TOKENS = {"low": 600, "typical": 1500, "high": 3000}

# ---------------------------------------------------------------------------
# RUBRIC
# Every check is a regular expression applied to the report text (case-insensitive).
# Sentences that are questions back to the user (ending in "?") are excluded from the
# fidelity checks, so "Is there calcification?" is not counted as an invented finding.
# ---------------------------------------------------------------------------

RUBRIC = {
    # ORGANIZATION: the seven sections of a standard report, plus their order.
    # The title is the first non-empty line if it names the exam (e.g. "CT CHEST").
    # Every other section is a heading: a line that starts with the section name
    # (optionally numbered, bold, or followed by a colon). Score = passed checks / 8.
    # A section being present is organization; what it says is checked under fidelity
    # (e.g. a Technique section that states contrast use the prompt never gave).
    "organization": {
        "title": r"\bCT\b|tomograph|scan",
        "title_max_words": 8,
        "sections": {
            "clinical_information": r"^[\s#>*_\-\d.]*(clinical (information|history|indication|data)|indications?|history|reason for (exam|study))\b",
            "technique": r"^[\s#>*_\-\d.]*(technique|protocol|procedure)\b",
            "comparison": r"^[\s#>*_\-\d.]*(comparisons?|prior (studies|exams?))\b",
            "findings": r"^[\s#>*_\-\d.]*findings\b",
            "impression": r"^[\s#>*_\-\d.]*(impression|diagnostic impression|conclusion|opinion)s?\b",
            "recommendation": r"^[\s#>*_\-\d.]*(recommendations?|plan)\b",
        },
        # Order: clinical information, technique and comparison (in any order among
        # themselves) before findings, then impression, then recommendation.
        "order_groups": [["clinical_information", "technique", "comparison"], ["findings"], ["impression"], ["recommendation"]],
    },
    # COMPLETENESS: every element supplied in the prompt must be carried into the report,
    # plus a follow-up interval that matches Fleischner 2017 for a single 6-8 mm solid
    # nodule (CT at 6-12 months) with the guideline named. Score = elements found / 12.
    "completeness": {
        "age": r"\b70\b|seventy",
        "sex": r"\b(male|man)\b",
        "smoking_history": r"never[- ]?smok|non[- ]?smoker|nonsmoker",
        "indication": r"chronic cough",
        "size": r"\b7\s*-?\s*mm\b|\b7\s*millimet",
        "attenuation": r"\bsolid\b",
        "number": r"\b(single|solitary)\b",
        "location": r"right lower lobe|\bRLL\b",
        "margins": r"smooth",
        "comparison": r"no (prior|previous|comparison)|not available for comparison|none available|no comparison",
        "followup_interval": r"6\s*(?:-|–|—|to)\s*12\s*months?",
        "guideline_named": r"fleischner",
    },
    # FIDELITY: anything the prompt did not supply. Each category counts at most once per
    # report. A report is faithful when no category is triggered. Score = 1 if faithful.
    "fidelity": {
        # Statements about anatomy the prompt never described (pertinent negatives included:
        # "No pleural effusion" is invented, because the prompt said nothing about the pleura).
        "invented_other_findings": {
            "pleura": r"pleura|effusion|pneumothorax",
            "lymph_nodes": r"lymph|adenopath|mediastin|hilar|hila\b",
            "lung_parenchyma_other": r"consolidation|emphysema|interstitial|fibros|ground[- ]glass opacit|atelecta|bronchiectas|no (other|additional) (pulmonary )?nodul",
            "airways": r"airway|trachea|bronch(?!iectas)",
            "heart_vessels": r"\bheart\b|cardiac|pericardi|aort|pulmonary arter|great vessel|coronary",
            "bones_chest_wall": r"osseous|\bbones?\b|skeletal|\bribs?\b|\bspine\b|vertebra|chest wall|soft tissue",
            "upper_abdomen_thyroid": r"abdomen|liver|adrenal|spleen|kidney|thyroid",
            "global_negative": r"otherwise (unremarkable|normal|no )|no other (significant|relevant|acute)|unremarkable",
        },
        # Details about the nodule or exam that were not supplied.
        "invented_nodule_details": {
            "calcification": r"calcif",
            "fat": r"\bfat\b|fat[- ]containing|macroscopic fat",
            "morphology": r"spicul|lobulat|cavit|perifissural|pleural tag",
            "segment": r"(superior|basal|apical|anterior|posterior|lateral|medial)[- ]?(basal )?segment|\bS(6|7|8|9|10)\b",
            "density_hu": r"\bHU\b|hounsfield",
            "measurement_detail": r"long[- ]axis|short[- ]axis|average diameter|mean diameter",
        },
        # Facts that contradict the prompt.
        "altered_facts": {
            "wrong_side_or_lobe": r"left (lower|upper) lobe|right (upper|middle) lobe|\bLLL\b|\bLUL\b|\bRUL\b|\bRML\b",
            "wrong_attenuation": r"part[- ]solid|subsolid|ground[- ]glass nodule|semi[- ]?solid",
            # checked only on lines that are not about technique (e.g. "1 mm reconstructions")
            "wrong_size": r"(?<![\d.\-–])(?!7\b)\d{1,2}(\.\d)?\s*mm\b",  # ranges like "6–8 mm" are guideline categories
            "wrong_smoking": r"(?<!never )(?<!non-)(?<!non )\b(current|former|ex)[- ]?smoker|pack[- ]years?",
            "screening_guideline": r"lung[- ]rads",
        },
        # Reported separately, not part of the fidelity score: assumptions about technique
        # (contrast, reconstructions, dose) that the prompt did not state.
        "assumed_technique": r"multiplanar|reformat|reconstruct|contrast|low[- ]dose|thin[- ]section|slice thickness|collimation",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_env() -> None:
    """Read KEY=VALUE pairs from .env into os.environ (no external dependency)."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def model_settings(provider: str) -> dict:
    m = MODELS[provider]
    return {
        "provider": provider,
        "label": m["label"],
        "model": os.environ.get(m["model_env"], m["model_default"]),
        "effort": os.environ.get(m["effort_env"], m["effort_default"]),
        "route": os.environ.get(m["route_env"], m["route_default"]),
        "supports_seed": m["supports_seed"],
        "price_in": float(os.environ.get(f"{provider.upper()}_PRICE_IN", m["price_in"])),
        "price_out": float(os.environ.get(f"{provider.upper()}_PRICE_OUT", m["price_out"])),
    }


def raw_path(provider: str) -> Path:
    return RAW_DIR / f"{provider}.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def chat_body(effort: str, messages: list[dict], seed: int | None = None,
              response_format: dict | None = None) -> dict:
    """Request body shared by the sync endpoint and the Batch API."""
    body = {"messages": messages, "max_tokens": MAX_OUTPUT_TOKENS, "reasoning": {"effort": effort}}
    if seed is not None:
        body["seed"] = seed
    if response_format is not None:
        body["response_format"] = response_format
    return body


def parse_chat(resp: dict) -> dict:
    """Pull the fields we log out of a chat-completion response (sync or batch)."""
    choice = resp["choices"][0]
    usage = resp.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "text": (choice.get("message") or {}).get("content") or "",
        "status": choice.get("finish_reason"),
        "model_returned": resp.get("model"),
        "provider_served": resp.get("provider"),
        "response_id": resp.get("id"),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),  # includes reasoning tokens
        "reasoning_tokens": details.get("reasoning_tokens"),
        "cost_usd": usage.get("cost"),
    }


def openrouter_chat(model: str, effort: str, route: str, messages: list[dict],
                    seed: int | None = None, response_format: dict | None = None) -> dict:
    """One synchronous chat completion through OpenRouter, pinned to a single provider.
    Full price; used for quick pilots (`--mode sync`). The main run uses the Batch API."""
    from openai import OpenAI

    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    body = chat_body(effort, messages, seed, response_format)
    extra_body = {
        "reasoning": body.pop("reasoning"),
        # Only the pinned provider; no silent switch to another host or model.
        "provider": {"only": [route], "allow_fallbacks": False, "require_parameters": True},
        "usage": {"include": True},  # returns the billed cost per call
    }
    t0 = time.time()
    resp = client.chat.completions.create(model=model, extra_body=extra_body, **body)
    out = parse_chat(resp.model_dump())
    out["latency_s"] = round(time.time() - t0, 2)
    return out


# ---------------------------------------------------------------------------
# OpenRouter Batch API: same models and endpoints, about half the price,
# results within 24 hours (usually minutes). One batch per model per step.
# ---------------------------------------------------------------------------

BATCH_STATE = HERE / "results" / "batches.json"
BATCH_POLL_SECONDS = 60


def or_http(method: str, path: str, payload: dict | None = None) -> dict:
    import urllib.request

    req = urllib.request.Request(
        OPENROUTER_BASE_URL + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def run_batch(key: str, model: str, route: str, items: list[tuple[str, dict]], on_result) -> None:
    """Submit (or resume) one batch, wait for it, and hand each result to on_result.

    on_result(custom_id, response_body_or_None, error_message_or_None)
    The batch id is saved before waiting, so an interrupted run resumes the same batch.
    """
    job = ensure_submitted(key, model, route, items)
    if job is None:
        return
    state = json.loads(BATCH_STATE.read_text())
    _ = state  # (state is re-read below when the job is finalized)
    import urllib.error

    misses = 0
    while True:
        try:
            b = or_http("GET", f"/batches/{job['id']}")
        except urllib.error.HTTPError as exc:
            # Right after submitting, the batch can briefly return 404; 429/5xx are transient.
            misses += 1
            if exc.code not in (404, 429, 500, 502, 503, 504) or misses > 30:
                raise
            print(f"  status check returned {exc.code}; retrying in {BATCH_POLL_SECONDS}s")
            time.sleep(BATCH_POLL_SECONDS)
            continue
        counts = b.get("request_counts") or {}
        print(f"  [{key}] {b['status']}: {counts.get('completed', 0)}/{counts.get('total', '?')} done, "
              f"{counts.get('failed', 0)} failed ({now()})")
        if b["status"] in ("completed", "failed", "expired", "cancelled") and (b["status"] != "completed" or b.get("results") is not None):
            break
        time.sleep(BATCH_POLL_SECONDS)
    return finish_batch(key, job, b, items, on_result)


def ensure_submitted(key: str, model: str, route: str, items: list[tuple[str, dict]]) -> dict | None:
    """Return the pending batch for this step, submitting it first if there is none.
    A batch that is already submitted (and not yet collected) is never submitted again."""
    BATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(BATCH_STATE.read_text()) if BATCH_STATE.exists() else {}
    job = state.get(key)
    if not job or job.get("collected"):
        if not items:
            return None
        # Field order matters to OpenRouter: everything else before "requests".
        payload = {
            "endpoint": "/v1/chat/completions",
            "model": model,
            "provider": {"only": [route], "allow_fallbacks": False},
            "completion_window": "24h",
            "requests": [{"custom_id": cid, "body": body} for cid, body in items],
        }
        sub = or_http("POST", "/batches", payload)
        job = {"id": sub["id"], "model": model, "route": route, "n_requests": len(items),
               "submitted": now(), "collected": False}
        state[key] = job
        BATCH_STATE.write_text(json.dumps(state, indent=2))
        print(f"  [{key}] submitted batch {job['id']} ({len(items)} requests)")
    else:
        print(f"  [{key}] batch {job['id']} already submitted; waiting for it")
    return job


def finish_batch(key: str, job: dict, b: dict, items: list[tuple[str, dict]], on_result) -> None:
    state = json.loads(BATCH_STATE.read_text())

    if b["status"] != "completed":
        msg = (b.get("error") or {}).get("message", b["status"])
        for cid, _ in items:
            on_result(cid, None, f"batch {b['status']}: {msg}")
    else:
        for res in b["results"]:
            err = res.get("error")
            body = (res.get("response") or {}).get("body")
            on_result(res["custom_id"], body, None if body and not err else f"{(err or {}).get('type')}: {(err or {}).get('message')}")
    job.update({"collected": True, "status": b["status"], "provider": b.get("provider"),
                "model_resolved": b.get("model"),  # dated snapshot actually served
                "usage": b.get("usage"), "finalized": now()})
    state[key] = job
    BATCH_STATE.write_text(json.dumps(state, indent=2))
    cost = (b.get("usage") or {}).get("cost")
    print(f"  batch {b['status']}" + (f", billed ${cost:,.4f}" if cost is not None else ""))


def generate(settings: dict, run_index: int) -> dict:
    # Fixed seed per run where the model supports it (GPT); Claude has no seed parameter.
    seed = SEED_BASE + run_index if settings["supports_seed"] else None
    out = openrouter_chat(settings["model"], settings["effort"], settings["route"],
                          [{"role": "user", "content": PROMPT}], seed=seed)  # sync path
    out["seed"] = seed
    if out["status"] != "stop" or not out["text"].strip():
        raise RuntimeError(f"finish_reason={out['status']}, empty={not out['text'].strip()}")
    return out


def one_call(provider: str, settings: dict, run_index: int) -> dict:
    last_error = None
    for attempt in range(1, 4):
        try:
            result = generate(settings, run_index)
            return {
                "run_index": run_index, "provider": provider,
                "model_requested": settings["model"], "effort": settings["effort"],
                "prompt_sha256": PROMPT_SHA256, "max_output_tokens": MAX_OUTPUT_TOKENS,
                "timestamp": now(), "attempt": attempt, "error": None, **result,
            }
        except Exception as exc:  # recorded, retried with backoff
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(5 * attempt)
    return {
        "run_index": run_index, "provider": provider, "model_requested": settings["model"],
        "effort": settings["effort"], "prompt_sha256": PROMPT_SHA256, "timestamp": now(),
        "attempt": 3, "error": last_error, "text": None,
    }


def write_manifest(n: int, providers: list[str]) -> None:
    import importlib.metadata as md

    def version(pkg: str) -> str | None:
        try:
            return md.version(pkg)
        except md.PackageNotFoundError:
            return None

    manifest = {
        "created": now(),
        "prompt": PROMPT,
        "prompt_sha256": PROMPT_SHA256,
        "n_per_model": n,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "models": {p: model_settings(p) for p in providers},
        "python": sys.version,
        "platform": platform.platform(),
        "packages": {"openai": version("openai")},
        "api": OPENROUTER_BASE_URL,
        "rubric_sha256": hashlib.sha256(json.dumps(RUBRIC, sort_keys=True).encode()).hexdigest(),
        "judge": judge_settings(),
        "judge_rubric_sha256": hashlib.sha256((JUDGE_SYSTEM + json.dumps(JUDGE_CATEGORIES, sort_keys=True)).encode()).hexdigest(),
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))


def run_generation_batch(p: str, settings: dict, todo: list[int], submit_only: bool = False) -> None:
    def seed_for(i):
        return SEED_BASE + i if settings["supports_seed"] else None

    items = [(f"{p}-{i}", chat_body(settings["effort"], [{"role": "user", "content": PROMPT}], seed_for(i)))
             for i in todo]
    if submit_only:
        ensure_submitted(f"run:{p}", settings["model"], settings["route"], items)
        return
    with raw_path(p).open("a") as fh:
        def on_result(cid, body, error):
            i = int(cid.rsplit("-", 1)[1])
            rec = {"run_index": i, "provider": p, "model_requested": settings["model"],
                   "effort": settings["effort"], "route": settings["route"], "mode": "batch",
                   "seed": seed_for(i), "prompt_sha256": PROMPT_SHA256,
                   "max_output_tokens": MAX_OUTPUT_TOKENS, "timestamp": now(), "error": error}
            if body:
                rec.update(parse_chat(body))
                if rec["status"] != "stop" or not rec["text"].strip():
                    rec["error"] = f"finish_reason={rec['status']}, empty={not rec['text'].strip()}"
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
        run_batch(f"run:{p}", settings["model"], settings["route"], items, on_result)


def cmd_run(args) -> None:
    load_env()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    providers = args.providers
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY is empty. Fill it in experiment/.env first.")
    write_manifest(args.n, providers)

    if args.mode == "batch":
        # Submit every model's batch first so they run at the same time, then collect each.
        for p in providers:
            settings = model_settings(p)
            done = {r["run_index"] for r in read_jsonl(raw_path(p)) if not r.get("error")}
            run_generation_batch(p, settings, [i for i in range(args.n) if i not in done], submit_only=True)

    for p in providers:
        settings = model_settings(p)
        done = {r["run_index"] for r in read_jsonl(raw_path(p)) if not r.get("error")}
        todo = [i for i in range(args.n) if i not in done]
        print(f"{settings['label']} ({settings['model']} via {settings['route']}, effort={settings['effort']}, "
              f"{args.mode}): {len(done)} done, {len(todo)} to run")
        if args.mode == "batch":
            run_generation_batch(p, settings, todo)
            continue
        with ThreadPoolExecutor(max_workers=args.workers) as pool, raw_path(p).open("a") as fh:
            futures = [pool.submit(one_call, p, settings, i) for i in todo]
            for k, fut in enumerate(as_completed(futures), 1):
                rec = fut.result()
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                flag = "ERROR " + rec["error"] if rec.get("error") else f"{rec.get('output_tokens')} out tok"
                print(f"  [{k}/{len(todo)}] run {rec['run_index']}: {flag}")


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def cmd_estimate(args) -> None:
    load_env()
    print("Batch prices (OpenRouter Batch API). A sync pilot costs about twice as much per call.")
    print(f"Pre-run estimate for {args.n} calls per model "
          f"(input ≈ {EST_INPUT_TOKENS} tokens; output incl. reasoning = low/typical/high)\n")
    totals = {k: 0.0 for k in EST_OUTPUT_TOKENS}
    for p in args.providers:
        s = model_settings(p)
        row = []
        for k, out_tok in EST_OUTPUT_TOKENS.items():
            cost = args.n * (EST_INPUT_TOKENS * s["price_in"] + out_tok * s["price_out"]) / 1e6
            totals[k] += cost
            row.append(f"{k} ${cost:,.2f}")
        print(f"  {s['label']:<16} ({s['model']}, ${s['price_in']}/${s['price_out']} per 1M): " + " | ".join(row))
    js = judge_settings()
    n_reports = args.n * len(args.providers)
    row = []
    for k, out_tok in EST_JUDGE_OUTPUT_TOKENS.items():
        cost = n_reports * (EST_JUDGE_INPUT_TOKENS * js["price_in"] + out_tok * js["price_out"]) / 1e6
        totals[k] += cost
        row.append(f"{k} ${cost:,.2f}")
    print(f"  {'Judge':<16} ({js['model']}, {n_reports} reports, ≈{EST_JUDGE_INPUT_TOKENS} in tok): " + " | ".join(row))
    row = []
    for k, out_tok in EST_JUDGE_OUTPUT_TOKENS.items():
        cost = CONSISTENCY_SAMPLE * len(args.providers) * (EST_JUDGE_INPUT_TOKENS * js["price_in"] + out_tok * js["price_out"]) / 1e6
        totals[k] += cost
        row.append(f"{k} ${cost:,.2f}")
    print(f"  {'Judge pass 2':<16} ({CONSISTENCY_SAMPLE} reports per model, consistency check): " + " | ".join(row))
    print("  " + "-" * 60)
    print("  TOTAL            " + " | ".join(f"{k} ${v:,.2f}" for k, v in totals.items()))
    print("\nRun a pilot (`run --n 3`) then `cost` to replace these assumptions with measured usage.")


def call_cost(rec: dict, settings: dict) -> float:
    """Billed cost reported by OpenRouter; falls back to tokens × list price."""
    if rec.get("cost_usd") is not None:
        return float(rec["cost_usd"])
    return (rec["input_tokens"] * settings["price_in"] + rec["output_tokens"] * settings["price_out"]) / 1e6


def cmd_cost(args) -> None:
    load_env()
    grand, projected = 0.0, 0.0
    for p in args.providers:
        s = model_settings(p)
        recs = [r for r in read_jsonl(raw_path(p)) if not r.get("error")]
        if not recs:
            print(f"  {s['label']}: no completed calls yet")
            continue
        spent = sum(call_cost(r, s) for r in recs)
        per_call = spent / len(recs)
        mean_out = statistics.mean(r["output_tokens"] for r in recs)
        grand += spent
        projected += per_call * args.n
        print(f"  {s['label']:<16} {len(recs)} calls, mean output {mean_out:,.0f} tok, "
              f"spent ${spent:,.3f}, ${per_call:,.4f}/call → {args.n} calls ≈ ${per_call * args.n:,.2f}")
    js = judge_settings()
    jrecs = [r for f in sorted(JUDGE_DIR.glob("pass*/*.jsonl")) for r in read_jsonl(f) if not r.get("error")]
    if jrecs:
        spent = sum(call_cost(r, js) for r in jrecs)
        per_call = spent / len(jrecs)
        grand += spent
        projected += per_call * args.n * len(args.providers)
        print(f"  {'Judge':<16} {len(jrecs)} calls, spent ${spent:,.3f}, ${per_call:,.4f}/call → "
              f"{args.n * len(args.providers)} reports ≈ ${per_call * args.n * len(args.providers):,.2f}")
    print(f"  Spent so far ${grand:,.3f}; projected for {args.n} per model ≈ ${projected:,.2f}")
    if BATCH_STATE.exists():
        state = json.loads(BATCH_STATE.read_text())
        billed = [(k, (j.get("usage") or {}).get("cost")) for k, j in state.items()]
        known = [c for _, c in billed if c is not None]
        print("  Billed by OpenRouter per batch (authoritative): " +
              ", ".join(f"{k} ${c:,.4f}" if c is not None else f"{k} pending" for k, c in billed) +
              (f"; total ${sum(known):,.4f}" if known else ""))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_report(text: str) -> dict:
    flags = re.IGNORECASE | re.MULTILINE
    lines = text.splitlines()
    # Split into sentences (a question can wrap over several lines) and drop questions.
    sentences = re.split(r"(?<=[.?!])\s+|\n\s*\n|\n(?=\s*[-*•●\d]+[.)]?\s)", text)
    statement_text = "\n".join(x for x in sentences if not x.strip().endswith("?"))
    # Bracketed placeholders for the radiologist to fill in ("[Describe pleura]") are not
    # statements of findings, so they are removed before the fidelity checks.
    statement_text = re.sub(r"\[[^\]]*\]", " ", statement_text)
    # A section label left with no content after removing placeholders ("Heart and vessels:")
    # is a heading, not a finding.
    statement_text = "\n".join(l for l in statement_text.splitlines()
                               if not re.fullmatch(r"[\s#*_\-]*[A-Za-z ,/&()]+:[\s*_]*", l))

    # Organization
    org = RUBRIC["organization"]
    first_line = next((l.strip(" #*_") for l in lines if l.strip()), "")
    has_title = bool(re.search(org["title"], first_line, re.IGNORECASE)) and len(first_line.split()) <= org["title_max_words"]
    positions = {}
    for name, pattern in org["sections"].items():
        m = re.search(pattern, text, flags)
        positions[name] = m.start() if m else None
    # in order = every present section in an earlier group comes before every present
    # section in a later group; at least findings, impression and recommendation needed.
    group_pos = [[positions[n] for n in g if positions[n] is not None] for g in org["order_groups"]]
    in_order = all(positions[n] is not None for n in ("findings", "impression", "recommendation"))
    for earlier, later in zip(group_pos, group_pos[1:]):
        if earlier and later and max(earlier) > min(later):
            in_order = False
    org_checks = {"has_title": has_title}
    org_checks.update({f"has_{n}": positions[n] is not None for n in org["sections"]})
    org_checks["in_order"] = in_order
    org_score = sum(org_checks.values()) / len(org_checks)

    # Completeness
    comp_checks = {k: bool(re.search(p, text, flags)) for k, p in RUBRIC["completeness"].items()}
    comp_score = sum(comp_checks.values()) / len(comp_checks)

    # Fidelity
    fid = RUBRIC["fidelity"]
    fid_hits = {}
    non_technique_text = "\n".join(
        l for l in statement_text.splitlines() if not re.search(fid["assumed_technique"], l, flags))
    for group in ("invented_other_findings", "invented_nodule_details", "altered_facts"):
        for k, p in fid[group].items():
            if group == "altered_facts":
                # Hypotheticals ("If the nodule is subsolid, ...") don't alter supplied facts.
                target = "\n".join(l for l in (non_technique_text if k == "wrong_size" else statement_text).splitlines()
                                   if not re.search(r"\bif\b", l, re.IGNORECASE))
            else:
                target = statement_text
            m = re.search(p, target, flags)
            if m:
                fid_hits[f"{group}.{k}"] = m.group(0)
    faithful = not fid_hits
    technique = bool(re.search(fid["assumed_technique"], statement_text, flags))

    return {
        "organization": org_score,
        "organization_checks": org_checks,
        "completeness": comp_score,
        "completeness_checks": comp_checks,
        "fidelity": 1.0 if faithful else 0.0,
        "fidelity_issue_count": len(fid_hits),
        "fidelity_hits": fid_hits,
        "assumed_technique": technique,
        "asked_questions": any(x.strip().endswith("?") for x in sentences),
        "uses_placeholders": bool(re.search(r"\[[^\]]{3,}\]", text)),
        "word_count": len(text.split()),
    }


# ---------------------------------------------------------------------------
# MODEL AS JUDGE
# A second model reads each report and answers yes/no questions a radiologist can
# check. Every answer comes with a short verbatim quote from the report as evidence,
# so a reviewer can confirm or overturn it without re-reading the whole report.
# The judge is not told which model wrote the report.
# ---------------------------------------------------------------------------

JUDGE = {
    "model_env": "JUDGE_MODEL", "model_default": "anthropic/claude-opus-5.5",
    "effort_env": "JUDGE_EFFORT", "effort_default": "medium",
    "route_env": "JUDGE_ROUTE", "route_default": "anthropic",
    "price_in": 2.00, "price_out": 10.00,  # batch price
}
JUDGE_DIR = HERE / "results" / "judge"
CONSISTENCY_SAMPLE = 20     # reports per model re-judged in a consistency pass
CONSISTENCY_SEED = 2027


def judge_path(p: str, judge_pass: int = 1) -> Path:
    """Judge outputs are kept per pass and never overwritten: pass1 is the main scoring,
    later passes re-judge a fixed sample of the same reports to measure judge consistency."""
    return JUDGE_DIR / f"pass{judge_pass}" / f"{p}.jsonl"
EST_JUDGE_INPUT_TOKENS = 2000
EST_JUDGE_OUTPUT_TOKENS = {"low": 1000, "typical": 2000, "high": 4000}

# Each category: (metric, question, answer that counts as good).
# metric "info" is recorded for the radiologist but not part of any score.
JUDGE_CATEGORIES = {
    # ORGANIZATION: one question per section, plus order. Good = yes.
    "org_title": ("organization", "Does the report start with a title that names the examination (for example 'CT Chest')?", True),
    "org_clinical_information": ("organization", "Is there a section for clinical information or indication?", True),
    "org_technique": ("organization", "Is there a technique section?", True),
    "org_comparison": ("organization", "Is there a comparison section stating whether prior studies were available?", True),
    "org_findings": ("organization", "Is there a findings section?", True),
    "org_impression": ("organization", "Is there an impression (or conclusion) section?", True),
    "org_recommendation": ("organization", "Is there a recommendation section?", True),
    "org_order": ("organization", "Are the sections in conventional order: clinical information, technique and comparison first; then findings; then impression; then recommendation?", True),

    # COMPLETENESS: every supplied element is kept, and the guideline follow-up is right. Good = yes.
    "comp_age_sex": ("completeness", "Does the report state the patient's age (70) and sex (male)?", True),
    "comp_smoking": ("completeness", "Does the report state that the patient never smoked?", True),
    "comp_indication": ("completeness", "Does the report state the indication (chronic cough)?", True),
    "comp_comparison_status": ("completeness", "Does the report state that there is no prior CT for comparison?", True),
    "comp_size": ("completeness", "Does the report give the nodule size as 7 mm?", True),
    "comp_attenuation": ("completeness", "Does the report describe the nodule as solid?", True),
    "comp_number": ("completeness", "Does the report describe the nodule as single (solitary)?", True),
    "comp_location": ("completeness", "Does the report locate the nodule in the right lower lobe?", True),
    "comp_margins": ("completeness", "Does the report describe the margins as smooth?", True),
    "comp_risk_category": ("completeness", "Does the report state the patient's risk category for the guideline (low risk, based on never smoking)?", True),
    "comp_followup_correct": ("completeness", "Does the recommendation match Fleischner 2017 for a single solid 6-8 mm nodule in a low-risk patient: CT at 6-12 months, then consider CT at 18-24 months?", True),
    "comp_guideline_cited": ("completeness", "Does the report name the guideline it follows (Fleischner Society 2017)?", True),

    # FIDELITY: nothing invented or changed. Good = no.
    "fid_other_findings": ("fidelity", "Does the report state any finding, normal or abnormal, about a structure other than the nodule (for example pleura, lymph nodes, airways, heart, bones, upper abdomen, or other lung findings)?", False),
    "fid_global_normal": ("fidelity", "Does the report state that the rest of the study is normal or unremarkable (for example 'no other significant abnormalities')?", False),
    "fid_nodule_details": ("fidelity", "Does the report add nodule characteristics that were not supplied (for example calcification, fat, spiculation, segment, density in HU, how it was measured)?", False),
    "fid_technique": ("fidelity", "Does the report state technique details that were not supplied (for example contrast use, reconstructions, slice thickness, dose)?", False),
    "fid_clinical_history": ("fidelity", "Does the report state clinical history that was not supplied as if it were fact (for example no history of malignancy, no exposures, family history)? Assumptions that are explicitly labelled as assumptions do not count.", False),
    "fid_altered_facts": ("fidelity", "Does the report change any supplied fact (age, sex, smoking, indication, size, attenuation, number, lobe, margins, prior imaging)?", False),
    "fid_wrong_guideline": ("fidelity", "Does the report apply a guideline that does not fit an incidental nodule (for example Lung-RADS, which is for screening)?", False),
    "fid_unsupported_recommendation": ("fidelity", "Does the report recommend something the supplied information does not support (for example PET-CT, biopsy, contrast CT, or a shorter interval than the guideline)?", False),

    # INFORMATIONAL: recorded for review, not scored.
    "info_states_assumptions": ("info", "Does the report explicitly label any of its statements as assumptions (for example 'assuming low risk')?", True),
    "info_uses_placeholders": ("info", "Does the report leave placeholders for the radiologist to complete (for example '[specify segment]')?", True),
    "info_flags_missing_information": ("info", "Does the report point out or ask about information that was not supplied (for example contrast, other findings, segment, calcification)?", True),
}

JUDGE_SYSTEM = """You are an experienced thoracic radiologist auditing a CT report written by an AI system.

The AI system received only the SUPPLIED INFORMATION below; it had no images and no other data. Judge the REPORT strictly against the supplied information:
- Anything the report states that is not in the supplied information is unsupported, including normal findings ("no pleural effusion") and technique details.
- Questions the report asks the reader are not statements; do not count them as findings.
- Statements explicitly labelled as assumptions ("assuming low risk") are not stated as fact.
- Placeholders left for the radiologist to complete, usually in square brackets ("[describe pleura]", "[specify segment]"), are not statements of findings or technique.

Return one item per question, using the question's id exactly. For each question, answer true or false and give the shortest verbatim quote from the report that supports your answer. If the answer is false because something is absent, use an empty string as the quote. Do not explain beyond the quote."""


def judge_schema() -> dict:
    """Compact schema: a list of {id, answer, evidence}. (One named field per question
    makes the constrained-decoding grammar too large for the API.) Completeness of the
    list is checked in code by parse_verdicts()."""
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "answer": {"type": "boolean"},
                                   "evidence": {"type": "string"}},
                    "required": ["id", "answer", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["answers"],
        "additionalProperties": False,
    }


def parse_verdicts(text: str) -> dict:
    """Turn the judge's list into {question_id: {answer, evidence}} and check every question is answered."""
    items = json.loads(text)["answers"]
    verdicts = {it["id"]: {"answer": it["answer"], "evidence": it["evidence"]} for it in items if it["id"] in JUDGE_CATEGORIES}
    missing = set(JUDGE_CATEGORIES) - set(verdicts)
    if missing:
        raise ValueError(f"judge omitted {sorted(missing)}")
    return verdicts


def judge_settings() -> dict:
    return {
        "model": os.environ.get(JUDGE["model_env"], JUDGE["model_default"]),
        "effort": os.environ.get(JUDGE["effort_env"], JUDGE["effort_default"]),
        "route": os.environ.get(JUDGE["route_env"], JUDGE["route_default"]),
        "price_in": float(os.environ.get("JUDGE_PRICE_IN", JUDGE["price_in"])),
        "price_out": float(os.environ.get("JUDGE_PRICE_OUT", JUDGE["price_out"])),
    }


def judge_user_message(report: str) -> str:
    questions = "\n".join(f"- {k}: {q}" for k, (_, q, _) in JUDGE_CATEGORIES.items())
    return (f"SUPPLIED INFORMATION:\n{PROMPT}\n\nREPORT:\n<<<\n{report}\n>>>\n\n"
            f"QUESTIONS (answer every one):\n{questions}")


def judge_one(report: str, settings: dict) -> dict:
    out = openrouter_chat(
        settings["model"], settings["effort"], settings["route"],
        [{"role": "system", "content": JUDGE_SYSTEM},
         {"role": "user", "content": judge_user_message(report)}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "report_audit", "strict": True, "schema": judge_schema()}},
    )
    if out["status"] != "stop":
        raise RuntimeError(f"judge finish_reason={out['status']}")
    verdicts = parse_verdicts(out["text"])
    out["raw_text"] = out.pop("text")  # the judge's exact output, kept verbatim
    return {"verdicts": verdicts, **{f"judge_{k}" if k in ("model_returned", "provider_served", "response_id") else k: v
                                     for k, v in out.items()}}


def judge_request(report: str, settings: dict) -> dict:
    return chat_body(
        settings["effort"],
        [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": judge_user_message(report)}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "report_audit", "strict": True, "schema": judge_schema()}},
    )


def judge_batch(p: str, js: dict, todo: list[dict], out: Path, judge_pass: int, submit_only: bool = False) -> None:
    items = [(f"{p}-{r['run_index']}", judge_request(r["text"], js)) for r in todo]
    if submit_only:
        ensure_submitted(f"judge:pass{judge_pass}:{p}", js["model"], js["route"], items)
        return
    with out.open("a") as fh:
        def on_result(cid, body, error):
            rec = {"run_index": int(cid.rsplit("-", 1)[1]), "provider": p, "judge_pass": judge_pass,
                   "judge_model": js["model"], "judge_effort": js["effort"], "mode": "batch",
                   "timestamp": now(), "error": error}
            if body:
                parsed = parse_chat(body)
                try:
                    if parsed["status"] != "stop":
                        raise ValueError(f"finish_reason={parsed['status']}")
                    rec["verdicts"] = parse_verdicts(parsed["text"])
                except (ValueError, json.JSONDecodeError) as exc:
                    rec["error"] = f"judge output invalid: {exc}"
                rec.update({k: v for k, v in parsed.items() if k != "text"})
                rec["raw_text"] = parsed["text"]  # the judge's exact output, kept verbatim
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
        run_batch(f"judge:pass{judge_pass}:{p}", js["model"], js["route"], items, on_result)


def cmd_judge(args) -> None:
    load_env()
    if not os.environ.get("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY is empty. Fill it in experiment/.env first.")
    js = judge_settings()
    jp = args.judge_pass

    def judge_todo(p):
        reports = [r for r in read_jsonl(raw_path(p)) if not r.get("error") and r.get("text")]
        if jp > 1:
            # Consistency pass: the same fixed random sample of reports already judged in pass 1.
            import random
            judged1 = sorted({r["run_index"] for r in read_jsonl(judge_path(p)) if not r.get("error")})
            sample = set(random.Random(CONSISTENCY_SEED).sample(judged1, min(args.sample, len(judged1))))
            reports = [r for r in reports if r["run_index"] in sample]
        out = judge_path(p, jp)
        out.parent.mkdir(parents=True, exist_ok=True)
        done = {r["run_index"] for r in read_jsonl(out) if not r.get("error")}
        return [r for r in reports if r["run_index"] not in done][: args.n], out, done

    if args.mode == "batch":
        for p in args.providers:  # submit all first so the batches run at the same time
            todo, out, _ = judge_todo(p)
            judge_batch(p, js, todo, out, jp, submit_only=True)

    for p in args.providers:
        todo, out, done = judge_todo(p)
        print(f"Judging {MODELS[p]['label']} reports, pass {jp}, with {js['model']} (effort={js['effort']}, "
              f"{args.mode}): {len(done)} done, {len(todo)} to judge")
        if args.mode == "batch":
            judge_batch(p, js, todo, out, jp)
            continue

        def work(rec):
            for attempt in range(1, 4):
                try:
                    res = judge_one(rec["text"], js)
                    return {"run_index": rec["run_index"], "provider": p, "judge_pass": jp, "judge_model": js["model"],
                            "judge_effort": js["effort"], "timestamp": now(), "error": None, **res}
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
                    time.sleep(5 * attempt)
            return {"run_index": rec["run_index"], "provider": p, "timestamp": now(), "error": err}

        with ThreadPoolExecutor(max_workers=args.workers) as pool, out.open("a") as fh:
            for k, fut in enumerate(as_completed([pool.submit(work, r) for r in todo]), 1):
                rec = fut.result()
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                print(f"  [{k}/{len(todo)}] run {rec['run_index']}: {'ERROR ' + rec['error'] if rec['error'] else 'ok'}")


def judge_scores(verdicts: dict) -> dict:
    def good(k):
        _, _, good_answer = JUDGE_CATEGORIES[k]
        return verdicts[k]["answer"] == good_answer

    by_metric = {m: [k for k, (mm, _, _) in JUDGE_CATEGORIES.items() if mm == m]
                 for m in ("organization", "completeness", "fidelity")}
    return {
        "organization": sum(good(k) for k in by_metric["organization"]) / len(by_metric["organization"]),
        "completeness": sum(good(k) for k in by_metric["completeness"]) / len(by_metric["completeness"]),
        "fidelity": 1.0 if all(good(k) for k in by_metric["fidelity"]) else 0.0,
    }


def cmd_review(args) -> None:
    """Write a review sheet for a radiologist: a random, fixed-seed sample of reports."""
    import random

    review_dir = OUT_DIR / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in args.providers:
        reports = {r["run_index"]: r for r in read_jsonl(raw_path(p)) if not r.get("error")}
        judged = {r["run_index"]: r for r in read_jsonl(judge_path(p)) if not r.get("error")}
        ids = sorted(set(reports) & set(judged))
        sample = sorted(random.Random(args.seed).sample(ids, min(args.sample, len(ids))))
        for i in sample:
            v = judged[i]["verdicts"]
            md = [f"# {MODELS[p]['label']}: run {i}", "", "## Report", "", "```", reports[i]["text"].strip(), "```", "",
                  "## Judge answers", "",
                  "Mark **Agree** or **Disagree** for each row. Good = the answer a correct report should get.", "",
                  "| Category | Question | Judge | Good | Evidence | Agree? |", "|---|---|---|---|---|---|"]
            for k, (metric, q, good_answer) in JUDGE_CATEGORIES.items():
                ans = v[k]["answer"]
                ev = v[k]["evidence"].replace("|", "/").replace("\n", " ")
                md.append(f"| {k} | {q} | {'yes' if ans else 'no'} | {'yes' if good_answer else 'no'} | {ev} | ☐ |")
                rows.append({"provider": p, "run_index": i, "category": k, "metric": metric, "question": q,
                             "judge_answer": ans, "good_answer": good_answer, "evidence": v[k]["evidence"],
                             "radiologist_agrees": "", "radiologist_note": ""})
            (review_dir / f"{p}_{i:03d}.md").write_text("\n".join(md) + "\n")
    if not rows:
        sys.exit("No judged reports yet. Run `experiment.py judge` first.")
    with (review_dir / "review_sheet.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows) // len(JUDGE_CATEGORIES)} reports to {review_dir} "
          f"(one .md per report, plus review_sheet.csv to record agreement).")


def summarize(values: list[float]) -> dict:
    return {
        "mean": round(statistics.mean(values), 3),
        "sd": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def cmd_score(args) -> None:
    load_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"prompt": PROMPT, "prompt_sha256": PROMPT_SHA256, "scored": now(), "models": {}}
    rows = []
    for p in args.providers:
        s = model_settings(p)
        recs = read_jsonl(raw_path(p))
        ok = [r for r in recs if not r.get("error") and r.get("text")]
        if not ok:
            continue
        judged = {r["run_index"]: r for r in read_jsonl(judge_path(p)) if not r.get("error")}
        scored, judge_rows = [], []
        for r in ok:
            sc = score_report(r["text"])
            scored.append(sc)
            j = judged.get(r["run_index"])
            if j:
                js_ = judge_scores(j["verdicts"])
                judge_rows.append({"regex": sc, "judge": js_, "verdicts": j["verdicts"]})
            row = {"provider": p, "run_index": r["run_index"], "model_returned": r.get("model_returned"),
                   "organization": sc["organization"], "completeness": sc["completeness"],
                   "fidelity": sc["fidelity"], "fidelity_issue_count": sc["fidelity_issue_count"],
                   "assumed_technique": sc["assumed_technique"], "asked_questions": sc["asked_questions"],
                   "uses_placeholders": sc["uses_placeholders"],
                   "word_count": sc["word_count"]}
            row.update({f"org.{k}": v for k, v in sc["organization_checks"].items()})
            row.update({f"comp.{k}": v for k, v in sc["completeness_checks"].items()})
            row["fidelity_hits"] = "; ".join(f"{k}='{v}'" for k, v in sc["fidelity_hits"].items())
            if j:
                row.update({f"judge.{k}": v for k, v in judge_rows[-1]["judge"].items()})
                row.update({f"judge.{k}": j["verdicts"][k]["answer"] for k in JUDGE_CATEGORIES})
            rows.append(row)

        def rate(key, sub=None):
            vals = [x[key][sub] if sub else x[key] for x in scored]
            return round(sum(bool(v) for v in vals) / len(vals), 3)

        hit_counts: dict[str, int] = {}
        for x in scored:
            for k in x["fidelity_hits"]:
                hit_counts[k] = hit_counts.get(k, 0) + 1

        summary["models"][p] = {
            "label": s["label"], "model": s["model"], "effort": s["effort"],
            "n_scored": len(scored), "n_errors": len(recs) - len(ok),
            "organization": summarize([x["organization"] for x in scored]),
            "completeness": summarize([x["completeness"] for x in scored]),
            "fidelity": summarize([x["fidelity"] for x in scored]),
            "fully_organized_rate": rate("organization_checks", "in_order"),
            "faithful_rate": rate("fidelity"),
            "assumed_technique_rate": rate("assumed_technique"),
            "asked_questions_rate": rate("asked_questions"),
            "placeholders_rate": rate("uses_placeholders"),
            "completeness_item_rates": {k: rate("completeness_checks", k) for k in RUBRIC["completeness"]},
            "fidelity_issue_rates": {k: round(v / len(scored), 3) for k, v in sorted(hit_counts.items(), key=lambda kv: -kv[1])},
        }
        if judge_rows:
            jn = len(judge_rows)
            summary["models"][p]["judge"] = {
                "judge_model": judge_settings()["model"], "n_judged": jn,
                "organization": summarize([x["judge"]["organization"] for x in judge_rows]),
                "completeness": summarize([x["judge"]["completeness"] for x in judge_rows]),
                "faithful_rate": round(sum(x["judge"]["fidelity"] for x in judge_rows) / jn, 3),
                "category_yes_rates": {k: round(sum(x["verdicts"][k]["answer"] for x in judge_rows) / jn, 3)
                                       for k in JUDGE_CATEGORIES},
                # How often the quick regex check and the judge reach the same verdict.
                "agreement_with_regex": {
                    "fidelity_pass": round(sum(x["regex"]["fidelity"] == x["judge"]["fidelity"] for x in judge_rows) / jn, 3),
                    **{f"org_{sec}": round(sum(x["regex"]["organization_checks"][f"has_{sec}"] == x["verdicts"][f"org_{sec}"]["answer"]
                                               for x in judge_rows) / jn, 3)
                       for sec in ("title", "clinical_information", "technique", "comparison", "findings", "impression", "recommendation")},
                    "org_order": round(sum(x["regex"]["organization_checks"]["in_order"] == x["verdicts"]["org_order"]["answer"]
                                           for x in judge_rows) / jn, 3),
                },
            }

    if not rows:
        sys.exit("Nothing to score yet. Run `experiment.py run` first.")

    with (OUT_DIR / "scores.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))

    md = ["# Base-prompt experiment: summary", "", f"Prompt: “{PROMPT}”", "",
          "| Model | n | Organization | Completeness | Fidelity (faithful rate) | Asked questions | Assumed technique |",
          "|---|---|---|---|---|---|---|"]
    for m in summary["models"].values():
        md.append(f"| {m['label']} ({m['model']}, {m['effort']}) | {m['n_scored']} | "
                  f"{m['organization']['mean']:.2f} ± {m['organization']['sd']:.2f} | "
                  f"{m['completeness']['mean']:.2f} ± {m['completeness']['sd']:.2f} | "
                  f"{m['faithful_rate']:.0%} | {m['asked_questions_rate']:.0%} | {m['assumed_technique_rate']:.0%} |")
    for m in summary["models"].values():
        md += ["", f"## {m['label']}", "", "Completeness items (share of reports that include each):", ""]
        md += [f"- {k}: {v:.0%}" for k, v in m["completeness_item_rates"].items()]
        md += ["", "Most common fidelity issues (share of reports):", ""]
        md += [f"- {k}: {v:.0%}" for k, v in list(m["fidelity_issue_rates"].items())[:10]] or ["- none"]
    judged_models = [m for m in summary["models"].values() if "judge" in m]
    if judged_models:
        md += ["", "## Model-as-judge results", "",
               "| Model | n judged | Organization | Completeness | Faithful rate | Agreement with regex (fidelity) |",
               "|---|---|---|---|---|---|"]
        for m in judged_models:
            j = m["judge"]
            md.append(f"| {m['label']} | {j['n_judged']} | {j['organization']['mean']:.2f} ± {j['organization']['sd']:.2f} | "
                      f"{j['completeness']['mean']:.2f} ± {j['completeness']['sd']:.2f} | {j['faithful_rate']:.0%} | "
                      f"{j['agreement_with_regex']['fidelity_pass']:.0%} |")
        for m in judged_models:
            md += ["", f"### {m['label']}: share of reports answered 'yes' (judge)", ""]
            md += [f"- {k}: {v:.0%}" for k, v in m["judge"]["category_yes_rates"].items()]
    (OUT_DIR / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    if not getattr(args, "no_archive", False):
        cmd_archive()

# ---------------------------------------------------------------------------
# Judge consistency: compare a later judge pass with pass 1 on the same reports.
# ---------------------------------------------------------------------------


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


def cmd_consistency(args) -> None:
    jp = args.judge_pass
    if jp < 2:
        sys.exit("Use --judge-pass 2 (or higher): pass 1 is compared with that pass.")
    result = {"compared": f"pass1 vs pass{jp}", "created": now(), "models": {}}
    md = [f"# Judge consistency: pass 1 vs pass {jp}", "",
          "Same reports, same judge, same prompt, judged twice. Agreement = share of identical answers.", ""]
    for p in args.providers:
        a = {r["run_index"]: r["verdicts"] for r in read_jsonl(judge_path(p, 1)) if not r.get("error")}
        b = {r["run_index"]: r["verdicts"] for r in read_jsonl(judge_path(p, jp)) if not r.get("error")}
        ids = sorted(set(a) & set(b))
        if not ids:
            continue
        per_cat = {}
        for k in JUDGE_CATEGORIES:
            xa = [a[i][k]["answer"] for i in ids]
            xb = [b[i][k]["answer"] for i in ids]
            per_cat[k] = {"agreement": round(sum(x == y for x, y in zip(xa, xb)) / len(ids), 3),
                          "kappa": cohen_kappa(xa, xb)}
        all_pairs = [(a[i][k]["answer"], b[i][k]["answer"]) for i in ids for k in JUDGE_CATEGORIES]
        sa = [judge_scores(a[i]) for i in ids]
        sb = [judge_scores(b[i]) for i in ids]
        result["models"][p] = {
            "label": MODELS[p]["label"], "n_reports": len(ids), "run_indices": ids,
            "overall_answer_agreement": round(sum(x == y for x, y in all_pairs) / len(all_pairs), 3),
            "fidelity_verdict_agreement": round(sum(x["fidelity"] == y["fidelity"] for x, y in zip(sa, sb)) / len(ids), 3),
            "organization_mean_abs_diff": round(statistics.mean(abs(x["organization"] - y["organization"]) for x, y in zip(sa, sb)), 3),
            "completeness_mean_abs_diff": round(statistics.mean(abs(x["completeness"] - y["completeness"]) for x, y in zip(sa, sb)), 3),
            "categories": per_cat,
        }
        m = result["models"][p]
        md += [f"## {m['label']} ({m['n_reports']} reports)", "",
               f"- All answers: {m['overall_answer_agreement']:.0%} identical",
               f"- Fidelity verdict (pass/fail): {m['fidelity_verdict_agreement']:.0%} identical",
               f"- Organization score: mean absolute difference {m['organization_mean_abs_diff']:.3f}",
               f"- Completeness score: mean absolute difference {m['completeness_mean_abs_diff']:.3f}", "",
               "| Category | Agreement | Cohen's κ |", "|---|---|---|"]
        md += [f"| {k} | {v['agreement']:.0%} | {'n/a' if v['kappa'] is None else v['kappa']} |" for k, v in per_cat.items()]
        md.append("")
    if not result["models"]:
        sys.exit(f"No reports judged in both pass 1 and pass {jp} yet.")
    (OUT_DIR / f"judge_consistency_pass{jp}.json").write_text(json.dumps(result, indent=2))
    (OUT_DIR / f"judge_consistency_pass{jp}.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


# ---------------------------------------------------------------------------
# Archive: a dated, read-only snapshot of results, code and settings.
# ---------------------------------------------------------------------------

ARCHIVE_DIR = HERE / "archive"
SECRET_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_archive(args=None) -> Path | None:
    """Copy results/, the code, docs and settings (API keys removed) into
    archive/<UTC timestamp>/, record the run dates and checksums, and make it read-only.
    Earlier snapshots are never modified."""
    import shutil
    import stat

    results = HERE / "results"
    if not results.exists():
        print("Nothing to archive yet (no results/ folder).")
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    dest = ARCHIVE_DIR / stamp
    k = 1
    while dest.exists():  # two snapshots in the same second
        k += 1
        dest = ARCHIVE_DIR / f"{stamp}-{k}"
    dest.mkdir(parents=True)
    shutil.copytree(results, dest / "results")
    for name in ("experiment.py", "requirements.txt", "RUBRIC.md", "README.md"):
        if (HERE / name).exists():
            shutil.copy2(HERE / name, dest / name)
    env = HERE / ".env"
    if env.exists():
        lines = [f"{l.split('=', 1)[0]}=<removed>" if l.split("=", 1)[0].strip() in SECRET_KEYS else l
                 for l in env.read_text().splitlines()]
        (dest / "settings.env").write_text("\n".join(lines) + "\n")

    # Run dates and what is inside.
    def window(paths):
        ts = sorted(r["timestamp"] for f in paths for r in read_jsonl(f) if r.get("timestamp"))
        return (ts[0], ts[-1]) if ts else (None, None)

    raw_files = sorted((results / "raw").glob("*.jsonl"))
    judge_files = sorted((results / "judge").glob("pass*/*.jsonl"))
    gen_start, gen_end = window(raw_files)
    judge_start, judge_end = window(judge_files)
    counts = {f"raw/{f.name}": sum(1 for r in read_jsonl(f) if not r.get("error")) for f in raw_files}
    counts.update({f"judge/{f.parent.name}/{f.name}": sum(1 for r in read_jsonl(f) if not r.get("error"))
                   for f in judge_files})
    batches = json.loads((results / "batches.json").read_text()) if (results / "batches.json").exists() else {}
    md = [f"# Archive {stamp}", "",
          f"- Archived: {now()}",
          f"- Generation calls: {gen_start} to {gen_end} (UTC)",
          f"- Judge calls: {judge_start} to {judge_end} (UTC)",
          f"- Prompt SHA-256: {PROMPT_SHA256}", "",
          "## Successful records", ""]
    md += [f"- {k}: {v}" for k, v in counts.items()]
    if batches:
        md += ["", "## OpenRouter batches", "", "| Step | Batch ID | Status | Provider | Billed (USD) | Submitted |", "|---|---|---|---|---|---|"]
        for k, j in batches.items():
            cost = (j.get("usage") or {}).get("cost")
            md.append(f"| {k} | {j.get('id')} | {j.get('status', 'pending')} | {j.get('provider')} | "
                      f"{'' if cost is None else f'{cost:.4f}'} | {j.get('submitted')} |")
    md += ["", "API keys were removed from settings.env. Files are read-only; SHA256SUMS lists every file's checksum."]
    (dest / "ARCHIVE.md").write_text("\n".join(md) + "\n")

    files = sorted(f for f in dest.rglob("*") if f.is_file())
    (dest / "SHA256SUMS").write_text("".join(f"{sha256_file(f)}  {f.relative_to(dest)}\n" for f in files))
    for f in dest.rglob("*"):  # read-only snapshot
        f.chmod(f.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    print(f"Archived to {dest.relative_to(HERE)} (read-only).")
    return dest


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("estimate", "run", "judge", "cost", "score", "review", "consistency", "archive"):
        sp = sub.add_parser(name)
        sp.add_argument("--n", type=int, default=N_DEFAULT, help="calls (or reports to judge) per model")
        sp.add_argument("--providers", nargs="+", default=list(MODELS), choices=list(MODELS))
        if name in ("run", "judge"):
            sp.add_argument("--mode", choices=["batch", "sync"], default="batch",
                            help="batch: OpenRouter Batch API, ~50%% cheaper, minutes to hours; "
                                 "sync: immediate, full price")
            sp.add_argument("--workers", type=int, default=4, help="parallel requests (sync mode)")
        if name in ("judge", "consistency"):
            sp.add_argument("--judge-pass", type=int, default=1,
                            help="1 = main scoring; 2+ = re-judge a fixed sample for consistency")
        if name == "judge":
            sp.add_argument("--sample", type=int, default=CONSISTENCY_SAMPLE,
                            help="reports per model re-judged in a consistency pass (pass 2+)")
        if name == "review":
            sp.add_argument("--sample", type=int, default=10, help="reports per model to review")
            sp.add_argument("--seed", type=int, default=2027, help="fixed seed for the sample")
        if name == "score":
            sp.add_argument("--no-archive", action="store_true", help="skip the automatic archive snapshot")
    args = ap.parse_args()
    {"estimate": cmd_estimate, "run": cmd_run, "judge": cmd_judge, "cost": cmd_cost,
     "score": cmd_score, "review": cmd_review, "consistency": cmd_consistency,
     "archive": cmd_archive}[args.cmd](args)


if __name__ == "__main__":
    main()
