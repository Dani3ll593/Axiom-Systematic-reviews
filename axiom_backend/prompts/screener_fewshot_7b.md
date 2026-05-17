### 🟢 EXAMPLE 1: CLEAR INCLUSION VIA SYNONYMS & INFERENCE

**INPUT CRITERIA (partial):** `{"population": {"include": ["adults with type 2 diabetes"]}, "intervention": {"include": ["machine learning", "artificial intelligence"]}, "study_design": {"include": ["observational", "RCT"]}}`

**INPUT ABSTRACT:** "We reviewed records of 450 T2DM patients over 18 years old. We applied a Random Forest classifier to predict glycemic control outcomes at 12 months."

**OUTPUT:**
```json
{
  "justification": "Population is T2DM patients over 18, which semantically matches adults with type 2 diabetes. Intervention is 'Random Forest classifier', which is a well-known machine learning method. Study design is implied observational by 'reviewed records'. All criteria are logically met without needing verbatim matches.",
  "criteria_met": {"population": true, "intervention": true, "outcomes": true, "study_design": true, "temporal": true, "language": true},
  "confidence": "high",
  "reason": null,
  "decision": "include"
}
```

---

### 🔴 EXAMPLE 2: CLEAR EXCLUSION (HIERARCHICAL)

**INPUT CRITERIA (partial):** `{"population": {"include": ["adults with type 2 diabetes"], "exclude": ["pediatric"]}, "study_design": {"exclude": ["case report"]}}`

**INPUT ABSTRACT:** "We present a clinical vignette of a pediatric patient with T1DM who responded well to pump therapy."

**OUTPUT:**
```json
{
  "justification": "Step 1 (Study Design): The abstract explicitly states 'clinical vignette' which is synonymous with 'case report', an excluded design. Exclusion confirmed at step 1.",
  "criteria_met": {"population": false, "intervention": false, "outcomes": false, "study_design": false, "temporal": true, "language": true},
  "confidence": "high",
  "reason": "wrong_study_design",
  "decision": "exclude"
}
```

---

### 🟡 EXAMPLE 3: GENUINE ESCALATION (CONFLICTING/MIXED DATA)

**INPUT CRITERIA (partial):** `{"population": {"include": ["adults with type 2 diabetes"], "exclude": ["type 1 diabetes"]}, "intervention": {"include": ["machine learning"]}}`

**INPUT ABSTRACT:** "We developed a predictive neural network for a mixed cohort of 1000 diabetic patients (both insulin-dependent pediatric T1DM and adult T2DM cases) to forecast hospital readmissions. Accuracy was 85% overall.."

**OUTPUT:**
```json
{
  "justification": "Intervention is neural network (machine learning), which fits. However, the population is explicitly a mixed cohort of both T1DM (excluded) and T2DM (included). The abstract does not specify if the results are disaggregated by diabetes type. Cannot confidently decide inclusion or exclusion without full text.",
  "criteria_met": {"population": false, "intervention": true, "outcomes": true, "study_design": true, "temporal": true, "language": true},
  "confidence": "low",
  "reason": "unavailable_full_text",
  "decision": "uncertain"
}
```