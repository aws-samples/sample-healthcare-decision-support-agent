# Response Schema

## NudgeResponse

```python
NudgeResponse(
    status="success|partial|error",
    warnings=["guideline_search_unavailable"],
    patient_summary="~150 word clinical narrative",
    nudges=[
        Nudge(
            title="Consider GLP-1 receptor agonist",
            urgency="warning",  # informational|warning|urgent
            category="treatment_recommendations",  # See categories below
            nudge_type="medication_adjustment",
            action_type="order",
            rationale="Based on ADA 2024 guidelines...",
            grounding="guideline",
            guideline_citation={"source": "ADA 2024", "section": "9.2"},
            icd_codes=["E11.9"],
            cpt_codes=["99214"]
        )
    ],
    metadata=NudgeMetadata(
        model_version="claude-sonnet-4",
        processing_time_ms=28500,
        guidelines_used=["ADA_2024"]
    )
)
```

## Nudge Categories

| Category | Description | Example Nudge Types |
|----------|-------------|---------------------|
| `gaps_in_care` | Missing preventive care or screenings | sdoh_screening, health_maintenance, screening |
| `treatment_recommendations` | Medication or therapy suggestions | medication_adjustment, intervention_recommendation |
| `risk_alerts` | Critical clinical warnings | sepsis_risk, readmission_risk, critical_labs, stemi_alert |
| `follow_up_actions` | Required follow-up activities | abnormal_results, additional_testing |
| `community_data_integration` | External health data alerts | community_data_alert, external_ccda |
| `clinical_monitoring` | Ongoing patient monitoring | fluid_imbalance, condition_change |
| `operational_efficiency` | Workflow and discharge optimization | discharge_readiness, los_variation |
