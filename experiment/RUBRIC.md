# Scoring rubric: base-prompt experiment

This document explains how each report is scored. Every report is scored twice:

1. **Quick check (regex):** fixed word patterns, defined in the `RUBRIC` dictionary in [experiment.py](experiment.py). Free and exactly reproducible, but it only matches wording. Sections 1–3 below.
2. **Model as judge:** a second model answers yes/no questions about each report, quoting the report as evidence. It catches subtler problems, and a radiologist can check each answer. Section 4 below.

The three metrics follow the definitions in the abstract's Background:

| Metric | Definition in the abstract | What is checked |
|---|---|---|
| **Organization** | Required sections, in order | Title, clinical information, technique, comparison, findings, impression and recommendation are present, in conventional order |
| **Completeness** | No omitted findings or descriptors | Every element given in the prompt appears in the report, plus the correct guideline follow-up |
| **Fidelity** | No fabricated or altered content | The report states nothing the prompt did not supply, and changes nothing it did supply |

## Input

Every call sends exactly this prompt, with no system prompt or instructions:

> 70-year-old man, never smoked. Chest CT for chronic cough. Single 7 mm solid nodule, right lower lobe, smooth margins. No prior CT. Write the report.

The prompt describes **only** the nodule. It says nothing about the rest of the chest, so any statement about other structures, positive or negative, was not supplied.

## 1. Organization (0 to 1)

Eight checks, each worth ⅛:

| # | Section | Counted when |
|---|---|---|
| 1 | **Title** | The first line names the exam ("CT CHEST", "Non-contrast chest CT") in 8 words or fewer |
| 2 | **Clinical information** | A heading such as "Clinical information", "Clinical history", "Indication", "History" |
| 3 | **Technique** | A heading "Technique" (also "Protocol", "Procedure") |
| 4 | **Comparison** | A heading "Comparison" (also "Prior studies") |
| 5 | **Findings** | A heading "Findings" |
| 6 | **Impression** | A heading "Impression" (also "Diagnostic impression", "Conclusion", "Opinion") |
| 7 | **Recommendation** | A heading "Recommendation(s)" (also "Plan") |
| 8 | **Order** | Clinical information, technique and comparison (in any order among themselves) come before Findings, then Impression, then Recommendation. Requires at least Findings, Impression and Recommendation |

A heading is a line that starts with the section name, optionally after numbering, markdown symbols or bold markers (`**Findings:**`, `## Impression`, `3. Recommendations`).

**Organization only checks that a section exists, not what it says.** The prompt gives no technique details, so a well-organized report should still have a Technique section, but anything it states there ("with IV contrast", "multiplanar reformations") is unsupported. That is caught under fidelity (technique), not here. The ideal Technique line for this prompt says the details were not provided.

## 2. Completeness (0 to 1)

Twelve elements, each worth 1/12. The first ten are what the prompt supplied; the last two check the guideline follow-up.

| # | Element | Counted when the report contains |
|---|---|---|
| 1 | Age | "70" or "seventy" |
| 2 | Sex | "male" or "man" |
| 3 | Smoking history | "never smoked/smoker", "non-smoker" |
| 4 | Indication | "chronic cough" |
| 5 | Size | "7 mm" |
| 6 | Attenuation | "solid" |
| 7 | Number | "single" or "solitary" |
| 8 | Location | "right lower lobe" or "RLL" |
| 9 | Margins | "smooth" |
| 10 | Comparison | "no prior", "no previous", "no comparison", "not available for comparison" |
| 11 | Follow-up interval | "6–12 months" (any dash, or "6 to 12 months"): Fleischner 2017 for a single 6–8 mm solid nodule |
| 12 | Guideline named | "Fleischner" |

Completeness only checks that supplied information is **kept**. It does not reward extra detail, because extra detail the prompt didn't supply is a fidelity problem.

## 3. Fidelity (pass/fail per report; reported as the % of faithful reports)

A report **passes** when none of the categories below is triggered. Each category counts once per report, however often it appears, and the script records the exact matched words so every flag can be checked by hand in `results/scores.csv`.

**Placeholders are excluded.** Text in square brackets left for the radiologist to fill in ("[Describe pleura]", "[specify segment]") is not a statement and is removed before these checks. Its use is recorded separately as `uses_placeholders`.

**Questions are excluded.** Sentences ending in "?" (for example "Is there calcification?") are questions back to the radiologist, not statements, so they are never counted.

### 3a. Invented findings elsewhere in the chest

Any statement, including normal ones, about anatomy the prompt never described:

| Category | Example wording that triggers it |
|---|---|
| Pleura | "No pleural effusion or pneumothorax" |
| Lymph nodes | "No mediastinal or hilar lymphadenopathy" |
| Other lung findings | "No consolidation", "no emphysema", "no additional nodules" |
| Airways | "Central airways are patent" |
| Heart and vessels | "Heart size normal", "aorta normal caliber" |
| Bones and chest wall | "No osseous abnormality" |
| Upper abdomen, thyroid | "Visualized upper abdomen unremarkable" |
| Global negatives | "Otherwise unremarkable", "no other significant abnormalities" |

The prompt didn't say the rest of the study was normal, so the model has no basis for these statements. A radiologist signing this report would be asserting findings they never gave.

### 3b. Invented nodule details

| Category | Example wording |
|---|---|
| Calcification | "noncalcified" |
| Fat | "no internal fat" |
| Morphology | "no spiculation", "perifissural" |
| Segment | "superior segment", "S6" |
| Density | "HU" |
| Measurement method | "mean of long and short axis" |

### 3c. Altered facts

| Category | Example wording |
|---|---|
| Wrong lobe or side | "right upper lobe", "left lower lobe" |
| Wrong attenuation | "part-solid", "ground-glass nodule" |
| Wrong size | any size in mm other than 7 (lines about technique, such as "1 mm reconstructions", are skipped) |
| Wrong smoking history | "former smoker", "pack-years" |
| Wrong guideline | "Lung-RADS" (a screening system, not for incidental nodules) |

### Reported separately: assumed technique

Statements about how the exam was done ("multiplanar reformations", "with contrast", "low-dose", "thin-section") are not in the prompt either. They are reported as their own rate (`assumed_technique_rate`) instead of failing fidelity, because a technique line is a template convention rather than a clinical finding. You can decide whether to treat it as a fidelity failure.

## 4. Model as judge

A second model (default **Claude Opus 5.5, medium effort**, `anthropic/claude-opus-5.5` on OpenRouter; set `JUDGE_MODEL` in `.env`) reads each report and answers the yes/no questions below. It sees the supplied information and the report, but not which model wrote the report. For every answer it returns the shortest verbatim quote from the report as evidence, so a radiologist can confirm or overturn it quickly. The exact wording sent to the judge is `JUDGE_SYSTEM` and `JUDGE_CATEGORIES` in [experiment.py](experiment.py).

**Scores from the judge:**
- **Organization** = share of the 8 organization questions answered "good"
- **Completeness** = share of the 12 completeness questions answered "good"
- **Fidelity** = the report passes only if all 8 fidelity questions are answered "no"

### Organization (good answer: yes)

| ID | Question |
|---|---|
| org_title | Does the report start with a title that names the examination? |
| org_clinical_information | Is there a section for clinical information or indication? |
| org_technique | Is there a technique section? |
| org_comparison | Is there a comparison section stating whether prior studies were available? |
| org_findings | Is there a findings section? |
| org_impression | Is there an impression (or conclusion) section? |
| org_recommendation | Is there a recommendation section? |
| org_order | Are the sections in conventional order (clinical information, technique, comparison; findings; impression; recommendation)? |

### Completeness (good answer: yes)

| ID | Question |
|---|---|
| comp_age_sex | Does the report state the patient's age (70) and sex (male)? |
| comp_smoking | Does it state that the patient never smoked? |
| comp_indication | Does it state the indication (chronic cough)? |
| comp_comparison_status | Does it state that there is no prior CT for comparison? |
| comp_size | Does it give the nodule size as 7 mm? |
| comp_attenuation | Does it describe the nodule as solid? |
| comp_number | Does it describe the nodule as single (solitary)? |
| comp_location | Does it locate the nodule in the right lower lobe? |
| comp_margins | Does it describe the margins as smooth? |
| comp_risk_category | Does it state the patient's risk category for the guideline (low risk, never smoker)? |
| comp_followup_correct | Does the recommendation match Fleischner 2017 for a single solid 6–8 mm nodule in a low-risk patient (CT at 6–12 months, then consider CT at 18–24 months)? |
| comp_guideline_cited | Does it name the guideline (Fleischner Society 2017)? |

### Fidelity (good answer: no)

| ID | Question |
|---|---|
| fid_other_findings | Does it state any finding, normal or abnormal, about a structure other than the nodule? |
| fid_global_normal | Does it state that the rest of the study is normal or unremarkable? |
| fid_nodule_details | Does it add nodule characteristics that were not supplied (calcification, fat, spiculation, segment, HU, measurement method)? |
| fid_technique | Does it state technique details that were not supplied (contrast, reconstructions, slice thickness, dose)? |
| fid_clinical_history | Does it state clinical history that was not supplied as fact (no malignancy, no exposures, family history)? Labelled assumptions don't count. |
| fid_altered_facts | Does it change any supplied fact? |
| fid_wrong_guideline | Does it apply a guideline that doesn't fit an incidental nodule (for example Lung-RADS)? |
| fid_unsupported_recommendation | Does it recommend something the supplied information doesn't support (PET-CT, biopsy, contrast CT, a shorter interval)? |

### Informational (recorded, not scored)

| ID | Question |
|---|---|
| info_states_assumptions | Does it explicitly label any statements as assumptions? |
| info_uses_placeholders | Does it leave placeholders for the radiologist to complete? |
| info_flags_missing_information | Does it point out or ask about information that was not supplied? |

### Radiologist review

`experiment.py review` picks a fixed random sample of judged reports (10 per model by default, seed 2027) and writes:

- `results/review/<model>_<run>.md`: the full report followed by a table of every judge answer, its evidence quote, and an **Agree?** box.
- `results/review/review_sheet.csv`: the same answers, one row per question, with empty `radiologist_agrees` and `radiologist_note` columns to fill in.

The share of answers the radiologist agrees with is the judge's measured accuracy, which belongs in the exhibit alongside the scores.

### Judge vs quick check

`score` also reports how often the regex check and the judge reach the same verdict (`agreement_with_regex` in `summary.json`). Low agreement on a category means one of the two is wrong there: read those reports first.

## Other measures recorded

- **Asked questions:** the share of reports that ask the radiologist for missing information.
- **Word count** per report.

## Output

| File | Content |
|---|---|
| `results/raw/<provider>.jsonl` | One line per call: full report text, model returned by the API, tokens, latency, timestamp, any error |
| `results/manifest.json` | Prompt and its hash, model settings, package versions, Python version, rubric hash |
| `results/judge/pass<N>/<provider>.jsonl` | One line per judged report: every yes/no answer with its evidence quote, the judge's exact output, judge model, tokens. Pass 1 is the scoring; later passes re-judge a sample for consistency |
| `results/judge_consistency_pass<N>.md` | Agreement and Cohen's κ between pass 1 and pass N |
| `archive/<date>/` | Dated, read-only snapshots of everything above plus code and settings |
| `results/review/` | Sampled reports and the review sheet for the radiologist |
| `results/scores.csv` | One row per report: the three scores, every individual check, and the exact matched words for each fidelity flag |
| `results/summary.json`, `results/summary.md` | Mean ± SD per metric and model, item-level completeness rates, most common fidelity issues |

## Limitations

- **Pattern matching, not reading.** The rubric matches wording. It is fully reproducible but can miss a paraphrase or flag a harmless phrase. Before reporting results, open `scores.csv`, read a sample of reports (for example 10 per model), and confirm the flags are right. If a pattern is wrong, fix it in `RUBRIC` and re-run `score`: scoring never calls the API again, so the numbers update for free.
- **The judge can be wrong, and may favor its own model.** The default judge is Claude Opus 5.5, which is also one of the two models being tested. The questions are factual yes/no checks with quoted evidence, which limits this, but the radiologist review is what measures it. To remove the concern, judge with a model that is not under test (set `JUDGE_MODEL`).
- **API, not the ChatGPT or Claude apps.** Calls go through OpenRouter to each maker's API. The apps add their own instructions, so their answers can differ.
- **Not fully deterministic.** GPT-6.1 Sol runs with a fixed seed per run, which OpenAI treats as best effort; Claude has no seed. The 100 runs therefore measure the spread of answers. The raw outputs are saved, so the scores themselves are exactly reproducible.
