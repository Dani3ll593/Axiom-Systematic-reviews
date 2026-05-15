### 🟢 EXAMPLE 1: CLEAR INCLUSION

**INPUT CRITERIA (partial):** `{"population": {"include": ["adults with type 2 diabetes"]}, "intervention": {"include": ["machine learning"]}, "study_design": {"include": ["RCT"]}}`

**INPUT ABSTRACT:** "We conducted a randomized controlled trial of 450 adults with type 2 diabetes in Mexico to evaluate whether a machine learning model can predict HbA1c levels at 12 months."

**OUTPUT:**
```json
{
  "justification": "Study design is explicitly a randomized controlled trial [Source abstract]. Population is adults with type 2 diabetes (n=450). Intervention is a machine learning model. All PICOS criteria match with verbatim keywords.",
  "criteria_met": {"population": true, "intervention": true, "outcomes": true, "study_design": true, "temporal": true, "language": true},
  "confidence": "high",
  "reason": null,
  "decision": "include"
}
```

---

### 🔴 EXAMPLE 2: CLEAR EXCLUSION (HIERARCHICAL)

**INPUT CRITERIA (partial):** `{"population": {"include": ["adults with type 2 diabetes"], "exclude": ["pediatric"]}, "study_design": {"exclude": ["case report"]}}`

**INPUT ABSTRACT:** "We present a case report of a pediatric patient with type 1 diabetes who responded well to insulin pump therapy."

**OUTPUT:**
```json
{
  "justification": "Step 1 (Study Design): The abstract explicitly states 'case report' [Source abstract], which is in the exclusion list. Per hierarchical rule, exclusion is confirmed at step 1.",
  "criteria_met": {"population": false, "intervention": false, "outcomes": false, "study_design": false, "temporal": true, "language": true},
  "confidence": "high",
  "reason": "wrong_study_design",
  "decision": "exclude"
}
```

---

### 🟡 EXAMPLE 3: ESCALATE — INSUFFICIENT EVIDENCE

**INPUT CRITERIA (partial):** `{"population": {"include": ["adults with type 2 diabetes"]}, "intervention": {"include": ["machine learning", "predictive model"]}, "study_design": {"include": ["RCT", "observational cohort"]}}`

**INPUT ABSTRACT:** "This study analyzed clinical data from 380 diabetes patients at a tertiary hospital. We developed an algorithm to support clinical decisions. Results showed promising performance compared to baseline."

**OUTPUT:**
```json
{
  "justification": "Population mentions 'diabetes patients' without specifying type 1 vs type 2 or pediatric vs adult — both inclusion and exclusion criteria are at stake. Intervention is described only as 'an algorithm', which could be machine learning or rule-based — not verifiable from abstract. Study design is not stated. Cannot decide from abstract alone.",
  "criteria_met": {"population": false, "intervention": false, "outcomes": false, "study_design": false, "temporal": true, "language": true},
  "confidence": "low",
  "reason": "unavailable_full_text",
  "decision": "uncertain"
}
```

This is the correct action — do NOT guess. Marking `confidence: "low"` and `decision: "uncertain"` lets the second reviewer adjudicate.
