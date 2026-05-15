### 🟢 EXAMPLE 1: AGREEING WITH 7B — CONFIRMED EXCLUSION

**INPUT PRIOR VERDICT (7B):**
```json
{
  "decision": "uncertain",
  "confidence": "low",
  "criteria_met": {"study_design": false, "population": true, "intervention": false, "outcomes": false, "temporal": true, "language": true},
  "justification": "Study design unclear; intervention described vaguely as 'an algorithm'."
}
```

**INPUT ABSTRACT:** "Retrospective chart review of 200 diabetic patients at our hospital from 2018-2022. A heuristic algorithm based on clinical thresholds was applied to flag high-risk cases. We compared flagged vs unflagged groups."

**OUTPUT:**

<think>
The 7B flagged study_design as unclear and intervention as vague. Let me re-read.
- Study design: "Retrospective chart review" — this is observational but NOT a prospective observational cohort. It is closer to a case-control or descriptive design, neither of which is on the included list (which requires RCT or prospective observational cohort).
- Intervention: "heuristic algorithm based on clinical thresholds" — this is rule-based, NOT machine learning. The inclusion criteria require "machine learning" or "predictive model" (which in context implies a learned model, not hardcoded rules).
Both criteria fail. Hierarchically, study_design fails first → exclude with wrong_study_design.
</think>

<json>
{
  "justification": "The abstract describes a 'retrospective chart review' [Source abstract], which is not within the included study designs (RCT or prospective observational cohort). At step 1 of hierarchical elimination, study design fails. Additionally, the intervention is described as a 'heuristic algorithm based on clinical thresholds', which is rule-based and not machine learning.",
  "criteria_met": {"population": true, "intervention": false, "outcomes": false, "study_design": false, "temporal": true, "language": true},
  "confidence": "high",
  "reason": "wrong_study_design",
  "decision": "exclude"
}
</json>

---

### 🟢 EXAMPLE 2: OVERTURNING 7B — RESOLVED INCLUSION

**INPUT PRIOR VERDICT (7B):**
```json
{
  "decision": "uncertain",
  "confidence": "low",
  "criteria_met": {"study_design": false, "population": true, "intervention": true, "outcomes": true, "temporal": true, "language": true},
  "justification": "Study design not explicitly named; abstract uses 'prospective analysis' which is ambiguous."
}
```

**INPUT ABSTRACT:** "We prospectively enrolled and followed 612 adults newly diagnosed with type 2 diabetes for 24 months at three urban clinics. A gradient boosting model was trained to predict 12-month HbA1c trajectories from baseline features. Internal validation showed AUC 0.82."

**OUTPUT:**

<think>
The 7B flagged study_design as unclear. Let me look closely.
"Prospectively enrolled and followed 612 adults... for 24 months at three urban clinics" — this is unambiguously a prospective observational cohort study by methodology, even if the literal phrase "cohort study" never appears. The 7B was being conservative about the missing label; the inclusion criteria include "observational cohort" and the methodology fits exactly.
Other criteria: adults with type 2 diabetes (population ✓), gradient boosting model is a form of machine learning (intervention ✓), predicting HbA1c trajectory is a valid outcome (outcomes ✓). All four hierarchical checks pass.
</think>

<json>
{
  "justification": "The abstract describes 'prospectively enrolled and followed 612 adults newly diagnosed with type 2 diabetes for 24 months' [Source abstract] — this is by definition a prospective observational cohort study, which is an included design. The 7B's uncertainty arose from absence of the literal phrase 'cohort study', but the methodology is unambiguous. Intervention (gradient boosting model) is machine learning, population is adults with T2DM, outcome is HbA1c trajectory.",
  "criteria_met": {"population": true, "intervention": true, "outcomes": true, "study_design": true, "temporal": true, "language": true},
  "confidence": "high",
  "reason": null,
  "decision": "include"
}
</json>

---

### 🟡 EXAMPLE 3: GENUINELY UNDECIDABLE — RARE CASE

**INPUT PRIOR VERDICT (7B):**
```json
{
  "decision": "uncertain",
  "confidence": "low",
  "criteria_met": {"study_design": false, "population": false, "intervention": false, "outcomes": false, "temporal": true, "language": true},
  "justification": "Abstract is a single sentence; insufficient information across all dimensions."
}
```

**INPUT ABSTRACT:** "We report findings from our recent diabetes informatics work."

**OUTPUT:**

<think>
This abstract is a 9-word summary with literally no actionable methodological information. No population details, no design, no intervention specifics, no outcomes, no sample size. I cannot independently resolve this — full-text retrieval is the only path. Confirming uncertain with reason = unavailable_full_text is the honest call.
</think>

<json>
{
  "justification": "The abstract contains only a single sentence without any methodological detail (no population characteristics, design, intervention, or outcomes specified). Independent adjudication is not possible from the abstract alone; full-text review is required.",
  "criteria_met": {"population": false, "intervention": false, "outcomes": false, "study_design": false, "temporal": true, "language": true},
  "confidence": "low",
  "reason": "unavailable_full_text",
  "decision": "uncertain"
}
</json>
