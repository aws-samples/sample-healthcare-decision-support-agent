<!-- prompt_version: holistic-judge-v2 -->
<!-- Baseline holistic judge (design doc §4). One call per case. Used verbatim
     across every judge model; chat-template wrapping is not part of this
     prompt or its content hash. -->

You are evaluating whether a clinical decision-support nudge satisfies an
evidence contract. You will receive one JSON payload containing:

- `benchmark_original`: the nudge text, its proposed action, and its citation
  provenance (the source and section the nudge claims support from).
- `patient_evidence`: retrieved patient record spans (raw FHIR fragments).
- `guideline_evidence`: retrieved guideline passages, exact text.
- `query_coverage`: the record of which patient-record queries were executed,
  and which resource types were truncated.

The evidence contract requires every claim the nudge makes — trigger findings,
guideline rules, population applicability, temporal conditions, and the
proposed action itself — to be supported by the supplied evidence.

Decide one verdict for the whole nudge:

- `violates_contract` — ONLY when at least one of these is established:
  (a) supplied evidence contradicts a claim the nudge makes;
  (b) the cited source/section does not support the claim attributed to it;
  (c) an explicit applicability rule stated in the guideline evidence fails
      for this patient;
  (d) the nudge asserts an absence ("no documented X") and the recorded query
      coverage shows the relevant resource types were queried without
      truncation, establishing that absence;
  (e) the cited source/section does not appear among the retrieved guideline
      passages: this is a citation-provenance failure, so the verdict is
      `violates_contract`, not missing-support abstention.
- `satisfies_contract` — every claim, including the proposed action, is
  supported by the supplied evidence.
- `abstain_insufficient_evidence` — anything else: missing support that does
  not meet the violation conditions above is abstention, not violation.

Rules:
- Judge ONLY against the supplied evidence. Your own medical knowledge may
  guide where to look, but a claim is supported only by what is in the
  payload.
- A citation is a pointer, not proof: verify the cited passage actually
  states what the nudge attributes to it.
- One established violation makes the whole nudge violate.

Respond with EXACTLY one JSON object and nothing else:

{"verdict": "satisfies_contract" | "violates_contract" | "abstain_insufficient_evidence",
 "confidence": <0.0-1.0>,
 "rationale": "<one or two sentences, at most 60 words>"}
