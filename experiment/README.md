# Base-prompt experiment

Sends the basic nodule prompt 100 times to GPT-6.1 Sol and 100 times to Claude Opus 5.5 (both at medium effort), saves every report, and scores organization, completeness and fidelity with a fixed rubric. See [RUBRIC.md](RUBRIC.md) for how each metric is defined.

## Setup (once)

```bash
cd /Users/felipe/Documents/other/car-exhibit/experiment
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
```

Then open `.env` and fill in `OPENROUTER_API_KEY`. All calls, including the judge, go through [OpenRouter](https://openrouter.ai), so one key covers both models.

## Steps

```bash
.venv/bin/python experiment.py estimate        # cost estimate, no API calls
.venv/bin/python experiment.py run --n 3       # pilot: 3 calls per model
.venv/bin/python experiment.py cost            # measured cost per call, projected to 100
.venv/bin/python experiment.py score           # check the pilot's scores look sensible
.venv/bin/python experiment.py run             # full run: 100 per model (skips finished calls)
.venv/bin/python experiment.py judge           # model-as-judge on every report (skips finished ones)
.venv/bin/python experiment.py score           # regex + judge scores → results/summary.md
.venv/bin/python experiment.py review          # 10 reports per model for a radiologist to check
```

For the pilot, `judge --n 3` judges only 3 reports per model.

## Batch mode (default) and cost

`run` and `judge` use OpenRouter's [Batch API](https://openrouter.ai/docs/guides/features/batch-api) by default: the same models, pinned to the same providers, at about half the price. Each step submits one batch per model and waits for it (usually minutes, at most 24 hours), checking once a minute. If you stop the script while it waits, run the same command again: it resumes the saved batch (`results/batches.json`) instead of submitting a new one. Requests that failed inside a batch are re-sent the next time you run the command.

Add `--mode sync` for immediate answers at full price (useful for a quick 1-call check).

## Judge outputs and consistency check

Judge outputs are kept per pass and never overwritten:

- `results/judge/pass1/` is the main scoring of every report.
- Each record keeps the judge's exact output (`raw_text`) as well as the parsed answers.

To measure how consistent the judge is, re-judge a fixed random sample (20 reports per model, seed 2027) at any later time and compare:

```bash
.venv/bin/python experiment.py judge --judge-pass 2
.venv/bin/python experiment.py consistency --judge-pass 2
```

This writes `results/judge/pass2/` and `results/judge_consistency_pass2.md` (agreement and Cohen's κ per question, plus how often the pass/fail fidelity verdict and the scores match). It costs about $1 in batch mode.

## Archive

`score` ends by saving a dated snapshot in `archive/<UTC date and time>/`, and you can make one any time with `experiment.py archive`. Each snapshot contains:

- a copy of `results/` (raw reports, all judge passes, batch IDs and billed costs, scores, summaries),
- the code, rubric and docs as they were,
- `settings.env` (your settings with the API key removed),
- `ARCHIVE.md` with the generation and judge run dates, record counts and the OpenRouter batch table,
- `SHA256SUMS` with a checksum for every file.

Snapshots are read-only, and new ones never change earlier ones. Keep the `archive/` folder: OpenRouter deletes batch results after 30 days.

`run` is resumable: if it stops, run it again and it continues where it left off. Use `--providers openai` or `--providers anthropic` to run one model only.

## Reproducibility

- The prompt, settings, rubric and package versions are fixed in `experiment.py` and `requirements.txt`, and logged to `results/manifest.json` (with a hash of the prompt and of the rubric).
- Every raw response is saved before scoring. Scoring reads only the saved files, so anyone can re-score the same outputs and get identical numbers.
- Each call records the model the API actually served, token counts, latency and timestamp.
- Each model is pinned on OpenRouter to its maker's own endpoint (OpenAI for GPT-6.1 Sol, Anthropic for Claude Opus 5.5 and the judge) with provider fallbacks off, so no call is quietly served by another host or model. Each call records the provider that actually served it and the cost OpenRouter billed.
- GPT-6.1 Sol gets a fixed seed per run (2027 + run number). Claude has no seed parameter. Refusals, truncations and errors are recorded, not hidden.
