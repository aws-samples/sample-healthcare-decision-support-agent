# Pipeline Details

## Execution Flow

```
┌──────────────────────────────────────────────────────────────┐
│ generate_nudges(ccda_xml, visit_context, config)             │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│ 1. Pre-parse CCDA/FHIR → structured dict                     │
│    (Efficient: LLM gets parsed data, not raw XML)            │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│ 2. Store raw document in secure temp file                    │
│    (Accessible only through get_patient_data)                │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│ 3. Agent executes with tools:                                │
│    - search_guidelines (OpenSearch or ripgrep fallback)      │
│    - list_guidelines (catalog lookup)                        │
│    - calculate (fresh isolated Strands Shell + Lua per call) │
│    - get_patient_data(file_path=...) (optional raw access)   │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────┐
│ 4. Return NudgeResponse + cleanup temp file                  │
└──────────────────────────────────────────────────────────────┘
```

## Data Privacy

- **Temp files**: Created in system temp directory, cleaned up after each request
- **atexit handler**: Ensures cleanup even on unexpected termination
- **No persistence**: Raw patient data is never stored long-term
- **Calculator isolation**: Empty per-call VFS with no host binds, network, or credentials
