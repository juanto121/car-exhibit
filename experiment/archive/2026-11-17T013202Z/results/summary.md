# Base-prompt experiment: summary

Prompt: “70-year-old man, never smoked. Chest CT for chronic cough. Single 7 mm solid nodule, right lower lobe, smooth margins. No prior CT. Write the report.”

| Model | n | Organization | Completeness | Fidelity (faithful rate) | Asked questions | Assumed technique |
|---|---|---|---|---|---|---|
| GPT-6.1 Sol (openai/gpt-6.1-sol, medium) | 10 | 0.60 ± 0.05 | 0.76 ± 0.06 | 100% | 0% | 80% |
| Claude Opus 5.5 (anthropic/claude-opus-5.5, medium) | 10 | 0.90 ± 0.13 | 0.99 ± 0.03 | 0% | 0% | 100% |

## GPT-6.1 Sol

Completeness items (share of reports that include each):

- age: 0%
- sex: 0%
- smoking_history: 30%
- indication: 80%
- size: 100%
- attenuation: 100%
- number: 100%
- location: 100%
- margins: 100%
- comparison: 100%
- followup_interval: 100%
- guideline_named: 100%

Most common fidelity issues (share of reports):

- none

## Claude Opus 5.5

Completeness items (share of reports that include each):

- age: 100%
- sex: 90%
- smoking_history: 100%
- indication: 100%
- size: 100%
- attenuation: 100%
- number: 100%
- location: 100%
- margins: 100%
- comparison: 100%
- followup_interval: 100%
- guideline_named: 100%

Most common fidelity issues (share of reports):

- invented_other_findings.lung_parenchyma_other: 100%
- invented_nodule_details.calcification: 90%
- invented_nodule_details.morphology: 90%
- invented_nodule_details.fat: 80%
- invented_nodule_details.measurement_detail: 70%
- invented_other_findings.airways: 60%
- altered_facts.screening_guideline: 40%
- invented_other_findings.lymph_nodes: 30%
- invented_other_findings.upper_abdomen_thyroid: 30%
- invented_other_findings.pleura: 20%

## Model-as-judge results

| Model | n judged | Organization | Completeness | Faithful rate | Agreement with regex (fidelity) |
|---|---|---|---|---|---|
| GPT-6.1 Sol | 10 | 0.72 ± 0.05 | 0.82 ± 0.07 | 100% | 100% |
| Claude Opus 5.5 | 10 | 0.93 ± 0.09 | 0.93 ± 0.04 | 0% | 100% |

### GPT-6.1 Sol: share of reports answered 'yes' (judge)

- org_title: 100%
- org_clinical_information: 80%
- org_technique: 0%
- org_comparison: 100%
- org_findings: 100%
- org_impression: 100%
- org_recommendation: 0%
- org_order: 100%
- comp_age_sex: 0%
- comp_smoking: 30%
- comp_indication: 80%
- comp_comparison_status: 100%
- comp_size: 100%
- comp_attenuation: 100%
- comp_number: 100%
- comp_location: 100%
- comp_margins: 100%
- comp_risk_category: 80%
- comp_followup_correct: 100%
- comp_guideline_cited: 100%
- fid_other_findings: 0%
- fid_global_normal: 0%
- fid_nodule_details: 0%
- fid_technique: 0%
- fid_clinical_history: 0%
- fid_altered_facts: 0%
- fid_wrong_guideline: 0%
- fid_unsupported_recommendation: 0%
- info_states_assumptions: 10%
- info_uses_placeholders: 0%
- info_flags_missing_information: 0%

### Claude Opus 5.5: share of reports answered 'yes' (judge)

- org_title: 100%
- org_clinical_information: 100%
- org_technique: 100%
- org_comparison: 100%
- org_findings: 100%
- org_impression: 100%
- org_recommendation: 50%
- org_order: 90%
- comp_age_sex: 100%
- comp_smoking: 100%
- comp_indication: 100%
- comp_comparison_status: 100%
- comp_size: 100%
- comp_attenuation: 100%
- comp_number: 100%
- comp_location: 100%
- comp_margins: 100%
- comp_risk_category: 20%
- comp_followup_correct: 100%
- comp_guideline_cited: 100%
- fid_other_findings: 80%
- fid_global_normal: 0%
- fid_nodule_details: 90%
- fid_technique: 90%
- fid_clinical_history: 10%
- fid_altered_facts: 0%
- fid_wrong_guideline: 0%
- fid_unsupported_recommendation: 0%
- info_states_assumptions: 10%
- info_uses_placeholders: 100%
- info_flags_missing_information: 100%
