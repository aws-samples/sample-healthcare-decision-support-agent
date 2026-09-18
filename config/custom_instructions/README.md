# Custom Instructions

This directory is reserved for runtime custom instructions provided by clinicians or end-users at the point of care.

## Purpose

Custom instructions allow clinicians to provide context-specific guidance that modifies how nudges are generated for a particular patient or visit type. This is separate from the built-in specialty guidance in `prompts/specialties/`.

## Usage

Custom instructions can be provided via the API:

```python
visit_context = {
    "visit_type": "ambulatory",
    "specialty": "cardiology",
    "custom_instructions": """
    - This patient has a known penicillin allergy - avoid recommending amoxicillin
    - Patient prefers morning medication schedules
    - Focus on heart failure management for this visit
    """
}
```

## Built-in Specialty Guidance

For built-in specialty guidance (cardiology, endocrinology, pediatrics, etc.), see `prompts/specialties/`. The agent automatically loads appropriate specialty guidance based on visit context and patient characteristics.
