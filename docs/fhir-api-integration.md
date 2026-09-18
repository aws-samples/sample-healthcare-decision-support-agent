# FHIR API Integration — Design Document

> AWS HealthLake as the managed FHIR R4 data store, queried on demand by the agent
> through a SigV4-signed FHIR REST client.

## Motivation

The pipeline's default path ingests patient data as flat files (CCDA XML or FHIR
JSON bundles). The entire file is parsed and passed to the LLM as context. That
creates two problems:

1. **Context window overflow** — MIMIC-IV FHIR bundles range from 0.7-44 MB
   (avg 8 MB). A single patient can have 1,300+ Observations. Most of it is
   irrelevant to the current visit.
2. **Not representative of production** — a production deployment integrates with a
   FHIR API, not flat files. This sample demonstrates the API-based integration
   pattern.

The agent therefore issues targeted FHIR searches scoped by the visit context,
turning a 44 MB patient dump into a few KB of relevant data.

## Solution Overview

```
Agent (local or AgentCore Runtime)
  → @tool query_patient_fhir()
    → RestFHIRClient(datastore_endpoint, auth=SigV4RequestsAuth(...))
      → AWS HealthLake FHIR R4 datastore
```

Data gets in through HealthLake bulk import, not through the REST API:

```
MIMIC-IV on FHIR demo (primary)    gunzip           ┐
Synthea sample bundles (fallback)  bundle → NDJSON  ┴→ S3 → StartFHIRImportJob
```

Provisioning lives in `terraform/healthlake.tf`, feature-flagged behind
`healthlake_enabled` (default `false`). Operational detail is in
[fhir-api-usage.md](fhir-api-usage.md).

## Why HealthLake — and why this reverses an earlier decision

**An earlier revision of this document chose HAPI FHIR on ECS Fargate over
HealthLake, and this design deliberately reverses that.** The argument then was
portability: the customer would integrate with a standard FHIR REST API, and HAPI's
API-key/bearer-token auth is closer to that than HealthLake's SigV4, so the client
code would transfer with a two-line config change. HAPI was also far cheaper
(~$30-50/month for small Fargate + RDS).

That argument was sound for its goal. This sample now optimizes for a different
one: **demonstrating an AWS-native managed FHIR store**, with no server, no
database, no patching, and no container to operate. The trade-off, stated plainly:

| | HAPI FHIR on ECS (previous) | AWS HealthLake (current) |
|---|---|---|
| Cost | ~$30-50/month | **~$197/month idle, cannot be paused** |
| Operations | ECS task + RDS + ALB to run, patch, and size | Fully managed |
| Auth | API key / bearer token | **IAM SigV4** |
| Auth portability | Matches most third-party FHIR endpoints | AWS-specific |
| Bulk load | POST transaction bundles | `StartFHIRImportJob` over NDJSON |
| Provisioning | Deploy script, no Terraform | Terraform (`awscc` provider) |
| FHIR surface | Standard FHIR R4 REST | Standard FHIR R4 REST, with caveats below |

What we give up is auth portability, and we mitigate it structurally rather than
pretending it does not matter:

- **The client stays transport-generic.** `RestFHIRClient` takes an injected
  `requests.auth.AuthBase`. HealthLake support is one small module
  (`search/fhir_auth.py`) that wraps botocore's `SigV4Auth`. Pointing the same
  client at a bearer-token or API-key FHIR endpoint is a constructor argument.
- **The application exposes only HealthLake configuration.** `get_fhir_config()`
  returns a datastore endpoint and a region — no API-key or bearer-token surface.
  A reader retargeting the sample at their own FHIR server changes the config
  shape *and* the client construction, and we would rather they see that
  explicitly than inherit a config schema that implies more portability than the
  deployed system has.

What we gain is that the whole data path — provisioning, import, encryption,
authorization — is Terraform-managed AWS infrastructure with no operational
surface, which is what a reader of an AWS sample is here to see.

`hashicorp/aws` has no HealthLake resource (its own README says so), so the
datastore is created via the Cloud Control API provider
(`awscc_healthlake_fhir_datastore`). That is the only `awscc` resource in the stack.

### HealthLake behavioural caveats that shaped the client

| Behaviour | Consequence |
|---|---|
| SigV4 required | Auth seam; sign the final URL *including* query string; re-sign next-links verbatim |
| `_count` capped at 100 | Client clamps to 1..100 |
| `_total=estimate` invalid | Use `accurate` or `none` |
| `_sort` on date needs a datastore created after 2023-12-09 | Always create fresh; never reuse an old datastore |
| String search is case-sensitive | Name lookups behave differently from HAPI |
| Search is eventually consistent | Write-then-immediately-search can return nothing |
| PHI in URLs | `POST /<Type>/_search` is the default so ids stay out of access logs |
| Chained search 4xx above 100 recursion hits | Avoid chained searches on wide patients |
| 8 regions only | `us-east-1` (project default) is supported |

### The cost consequence, stated once more

HealthLake bills from create to delete with no pause: **~$0.27/hour, ~$197/month
idle**. That is the price of this decision and the main financial risk of the
sample. Mitigations: the datastore is behind a default-off feature flag, Terraform
creates a HealthLake-scoped AWS Budgets budget, the teardown command is a Terraform
output, and **file mode (CCDA/FHIR JSON) remains the zero-cost default path**.

## Data

Two datasets, both fetched by the reader rather than vendored in this repository.

| Dataset | Access | Packaging | Size | Role |
|---|---|---|---|---|
| MIMIC-IV Clinical Database Demo on FHIR v2.1.0 | **Open access, ODbL v1.0** — no account, no DUA | gzipped NDJSON, one file per resource profile | 28.9 MB zip / 49.5 MB uncompressed, 100 patients | **Primary** |
| Synthea sample FHIR R4 (nov2021) | Public GitHub, pinned at commit `a59acd4` | per-patient transaction Bundles + 2 companion batch Bundles | ~95 MB zip, 555 patients | Fallback |
| MIMIC-IV on FHIR (full) | **Credentialed** (PhysioNet DUA) | same as demo | — | Bring-your-own only; never mirrored |

MIMIC-IV is the primary dataset because Synthea data is too clean to be
representative: real charting is messy, sparsely coded, and full of the
inconsistencies a clinical decision-support agent has to survive. Synthea remains a
documented fallback for readers who want zero third-party dependencies.

### Licensing posture

- **Do not vendor MIMIC data.** ODbL §4.4 share-alike and §4.6 (offer recipients the
  derivative database or the alterations/method) would attach to any mirrored copy,
  turning this into a mixed-license repository. `scripts/mimic/fetch_demo.py`
  downloads it instead.
- Publishing metrics or example model outputs derived from the demo is permitted —
  those are ODbL "Produced Works", exempt from share-alike — but requires the §4.3
  notice: *"Contains information from MIMIC-IV Clinical Database Demo on FHIR, which
  is made available here under the Open Database License (ODbL)."* plus the
  requested citations.
- Tooling that reads or transforms MIMIC data is code, not data, and ships under this
  repository's own license.
- The full dataset's credentialed license bars sharing access with non-credentialed
  parties and restricts use to scientific research. It is supported only as
  "point the tool at your own local copy".

### Why bulk import, not the REST API

HealthLake bulk import is **NDJSON, upsert-by-id**, which is a better fit than
posting Bundles:

- Both datasets already carry stable, deterministic resource ids (MIMIC uses
  UUIDv5 derived from source ids; the pinned Synthea artifact is static), so an
  import is idempotent and re-runnable.
- Transaction Bundles POSTed to a FHIR server assign *new* ids, which destroys
  reproducibility across loads.
- Bundles cap at 500 resources and a 5 MB sync payload; import handles 50 GB files.

Consequences the pipeline encodes:

- `ValidationLevel=structure-only`. MIMIC declares its own IG profiles
  (`http://mimic.mit.edu/fhir/mimic/StructureDefinition/...`) that HealthLake does
  not support, and the default `strict` level rejects them en masse. The same
  setting harmlessly ignores Synthea's US Core `meta.profile` claims, so one code
  path covers both sources.
- **No load-order tiering.** Import applies each resource as an independent
  upsert-by-id and stores reference strings literally, so dangling references
  import cleanly and there is nothing to sequence. The previous HAPI loader's
  dependency tiers and its per-patient microbiology transaction merge both
  disappear.
- **1 concurrent import job per region**, so jobs are serialized explicitly rather
  than left to server-side queuing.
- `JobOutputDataConfig.S3Configuration.KmsKeyId` is **required**, so a KMS key is
  provisioned even for a throwaway sample.

### Normalization per source

**MIMIC** — gunzip. That is the whole conversion. Optional filters:
`--subset core` drops `MimicObservationChartevents` (35 MB of the 49.5 MB), and
`--patients-file` keeps only a curated cohort.

**Synthea** — unwrap `entry.resource`, then repair references:
1. `"reference": "urn:uuid:<id>"` → `"<Type>/<id>"` using an id→type map built over
   the whole corpus.
2. Conditional references (`Practitioner?identifier=<system>|<value>`) → `<Type>/<id>`
   using an identifier map built from the companion `hospitalInformation*` and
   `practitionerInformation*` bundles.

Step 2 is a **data-quality** step, not an import gate: unresolved conditional
references import fine, they simply never resolve on a read. The converter reports
residual counts rather than failing, and the CLI warns when any remain.

### Scenario curation happens before import

`select_mimic_scenarios.py` streams the NDJSON, accumulates per-patient clinical
flags, and writes a scenario manifest. `curate_mimic_experiment.py` turns that into a
seeded, balanced cohort file. `healthlake_import.py --patients-file` then imports only
that cohort. Filtering before import (rather than selecting at query time) keeps the
billed datastore small and needs no assembled per-patient bundles or symlink trees.

## Agent Integration

### How the agent queries patient data

```
Agent reasoning:
  "This is a cardiology follow-up. I need active conditions,
   current medications, recent cardiac labs, and vital signs."

  → Condition?patient=<id>&clinical-status=active
  → MedicationRequest?patient=<id>&status=active
  → Observation?patient=<id>&category=laboratory&_sort=-date&_count=20
  → Observation?patient=<id>&category=vital-signs&_sort=-date&_count=10
```

All four are issued as `POST /<Type>/_search` with form-encoded bodies, so patient
identifiers never appear in a URL — and therefore never in CloudTrail or access logs.

### The auth seam

```python
class SigV4RequestsAuth(requests.auth.AuthBase):
    def __call__(self, request):
        # request.url already carries the query string, request.body the exact bytes
        aws_request = AWSRequest(request.method, request.url, data=request.body, ...)
        SigV4Auth(self.credentials, "healthlake", self.region).add_auth(aws_request)
        request.headers.update(aws_request.headers)
        return request
```

Three details that are easy to get wrong and are covered by tests:

1. **`requests` prepares the URL before it prepares auth**, so the auth callable
   sees the full query string and SigV4 signs it. If that order were reversed every
   parameterized search would fail with a signature mismatch.
2. **Next-link pagination must re-sign the returned URL verbatim.** The URL carries
   an opaque continuation token that is part of the canonical request; rebuilding it
   from parsed params breaks the signature.
3. **The POST body must not be re-encoded after signing.** The client passes a
   pre-encoded `data=` string, never `json=`.

Credentials come from `boto3.Session().get_credentials()` so SSO/assume-role
credentials refresh instead of expiring mid-run.

### AgentCore deployment

The agent runs the `query_patient_fhir` tool in-process in both local and AgentCore
Runtime deployments, signing with the runtime's own IAM role. There is no API Gateway
and no ALB in front of the datastore — HealthLake *is* the endpoint, and IAM is the
authorization layer.

An AgentCore Gateway OpenAPI target is not used here: Gateway's API-key credential
flow does not produce SigV4 signatures, so fronting HealthLake would mean adding a
signing proxy — infrastructure that exists only to re-add what the in-process client
already does. If you need Gateway-managed MCP tools over a FHIR endpoint, that is a
better fit for a bearer-token FHIR server than for HealthLake.

## Configuration

### settings.yaml

```yaml
fhir_api:
  enabled: false            # Enable the HealthLake data source
  datastore_endpoint: ""    # terraform output healthlake_datastore_endpoint, verbatim
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `FHIR_API_ENABLED` | No | Enable the HealthLake data source |
| `HEALTHLAKE_DATASTORE_ENDPOINT` | When enabled | Datastore endpoint (`FHIR_API_BASE_URL` accepted as a legacy alias) |
| `AWS_REGION` | No | Region used for SigV4 signing |

No API key exists on this path: authorization is IAM. The agent's role needs
`healthlake:ReadResource`, `SearchWithGet`, `SearchWithPost`, and `GetCapabilities`
on the datastore ARN.

### Orchestrator usage

```python
# File-based (unchanged, zero cost)
orchestrator.generate_nudges(fhir_json=json_str)
orchestrator.generate_nudges(ccda_xml=xml_str)

# HealthLake-based
orchestrator.generate_nudges(
    visit_context={
        "patient_id": "0a8eebfd-a352-522e-89f0-1d4a13abdebc",
        "data_source": "fhir_api",
        "visit_type": "ambulatory",
        "specialty": "cardiology",
    }
)
```

## Scripts & Artifacts

| Script / File | Purpose |
|---|---|
| `terraform/healthlake.tf` | Datastore, KMS key, staging bucket, import role, Budgets guardrail |
| `scripts/mimic/fetch_demo.py` | Download the open-access MIMIC-IV-on-FHIR demo |
| `scripts/healthlake_import.py` | Normalize → S3 stage → `StartFHIRImportJob` |
| `src/medical_nudging/healthlake/normalize.py` | Per-source NDJSON normalization |
| `src/medical_nudging/healthlake/import_job.py` | Staging, job control, `Manifest.json` parsing |
| `scripts/mimic/select_mimic_scenarios.py` | Scenario flags from streamed NDJSON |
| `scripts/mimic/curate_mimic_experiment.py` | Seeded balanced cohort file |
| `scripts/mimic/assemble_mimic_bundles.py` | Per-patient Bundles for the zero-cost file path |
| `src/medical_nudging/search/fhir_client.py` | `RestFHIRClient`, `create_healthlake_client` |
| `src/medical_nudging/search/fhir_auth.py` | `SigV4RequestsAuth` |
| `src/medical_nudging/tools/fhir_query.py` | `@tool query_patient_fhir` |

## Design Decisions

### Pagination: follow next-links, with an explicit cap

The earlier design said "don't paginate — use filters instead", and the client
silently returned only the first page. That was a bug dressed as a decision: any
query issued without `_count` got the server's default page size and the caller
never learned there was more.

`search()` now follows `Bundle.link[relation="next"]` up to `max_pages`
(default 10) and logs a warning when it stops at the cap. Filters are still the
primary tool for keeping responses small — the agent is instructed to scope by date
and category — but truncation is now a logged choice rather than an accident.

### `_include` entries are filtered out

Entries whose `search.mode` is `"include"` are dropped, so referenced Patients or
Practitioners can never masquerade as search matches if `_include`/`_revinclude` is
ever adopted.

### `_count` is clamped, not validated

The agent can pass arbitrary search params. HealthLake caps page size at 100 and
rejects `_count=0`, so the client clamps into 1..100 and logs the adjustment rather
than letting a 4xx surface as a tool error.

### `data_source` precedence: explicit params win

If both `fhir_json` (file content) and `data_source: "fhir_api"` are passed, the
explicit file content wins and a warning is logged. Explicit data is concrete and
testable; `data_source` is a routing hint.

### Datastore behind a default-off flag

`healthlake_enabled` defaults to `false`. Every HealthLake resource is `count`-gated
on it. Readers who only want to see the agent work never create a billed datastore,
and readers who do get a Budgets guardrail and a teardown command by default.

## Customer Handoff Value

For a reader whose target is a third-party FHIR R4 endpoint (an EHR's FHIR API, a
health-data aggregator) rather than HealthLake:

1. **The query strategy transfers unchanged.** Targeted, visit-scoped searches over
   standard R4 search parameters are server-agnostic.
2. **The client transfers.** Swap the injected auth object —
   `RestFHIRClient(base_url, bearer_token=...)` or a custom `AuthBase` for OAuth2
   — and keep `read`, `search`, pagination, and Bundle handling.
3. **What does not transfer is the auth and provisioning layer**, and that is the
   deliberate cost of choosing a managed AWS store. The seam is small and isolated
   in `search/fhir_auth.py` precisely so the boundary is visible.
