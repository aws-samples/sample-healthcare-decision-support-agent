# FHIR API Integration — Usage & Verification

> AWS HealthLake datastore setup, bulk data import, and client usage.

## ⚠️ Cost first

A HealthLake datastore is billed from creation to deletion and **cannot be paused
or stopped**. At the published us-east-1 rate of $0.27 per datastore-hour:

| | |
|---|---|
| Hourly | ~$0.27 |
| Daily | ~$6.48 |
| Monthly (idle) | **~$197** |

The 100-patient MIMIC-IV demo fits inside the free 10 GB storage tier and the free
3,500 queries/hour allowance, so the bill is essentially the "it exists" charge.

**File mode (CCDA XML / FHIR JSON / pre-parsed JSON) costs nothing and remains the
default.** Only enable HealthLake when you are actively demonstrating the API path,
and destroy the datastore afterwards:

```bash
cd terraform
terraform destroy -target=awscc_healthlake_fhir_datastore.fhir \
  -var="healthlake_enabled=true"
```

`terraform apply` also creates an AWS Budgets budget scoped to HealthLake spend
(`healthlake_budget_alarm_enabled`, default `true`). Set
`healthlake_budget_alert_emails` to get notified at 80% forecasted / 100% actual.

## Prerequisites

- `uv` for Python script execution
- AWS credentials with permission to create HealthLake datastores, S3 buckets, KMS
  keys, and IAM roles (`terraform apply`), `ram:GetResourceShareInvitations` (the
  CreateFHIRDatastore call checks it on the caller), plus `iam:PassRole` scoped to
  `healthlake.amazonaws.com` for the import job. Both were exercised under the
  least-privilege policy in `terraform/deployer-policy.example.json`.
- HealthLake is available in 8 regions only: `us-east-1`, `us-east-2`, `us-west-2`,
  `ap-south-1`, `ap-southeast-2`, `ca-central-1`, `eu-west-1`, `eu-west-2`

## 1. Deploy the datastore

```bash
cd terraform
terraform apply -var="healthlake_enabled=true"
```

Datastore creation took 2 to 3 minutes in a fresh account (the service documents up to
30). When this stack creates the datastore, Terraform points the runtime at it in the
same apply (`FHIR_API_ENABLED`, `HEALTHLAKE_DATASTORE_ENDPOINT`). Local scripts still
read the endpoint from `config/settings.yaml`, so wire it there too:

```bash
terraform output healthlake_datastore_endpoint
# https://healthlake.us-east-1.amazonaws.com/datastore/<id>/r4/
```

Copy that value verbatim into `config/settings.yaml`:

```yaml
fhir_api:
  enabled: true
  datastore_endpoint: "https://healthlake.us-east-1.amazonaws.com/datastore/<id>/r4/"
```

or set `HEALTHLAKE_DATASTORE_ENDPOINT` in the environment. **Never assemble the URL
by hand** — read it from the datastore.

Terraform also emits everything the import pipeline needs:

| Output | Purpose |
|---|---|
| `healthlake_datastore_id` | Target of `StartFHIRImportJob` |
| `healthlake_staging_bucket` | `import/` for NDJSON, `output/` for job reports |
| `healthlake_import_role_arn` | Role HealthLake assumes to read/write S3 |
| `healthlake_import_kms_key_arn` | Required by `JobOutputDataConfig` |

Sanity-check auth and the FHIR surface with the CapabilityStatement:

```python
from medical_nudging.search.fhir_client import create_healthlake_client

client = create_healthlake_client(endpoint, region="us-east-1")
print(client.metadata()["resourceType"])  # → CapabilityStatement
```

## 2. Import patient data

### MIMIC-IV on FHIR demo (primary path)

Open access under ODbL v1.0 — no PhysioNet account, no CITI training, no DUA.
100 patients, 28.9 MB ZIP, 49.5 MB uncompressed.

```bash
# Download from PhysioNet (data is NOT vendored in this repo — see below)
uv run scripts/mimic/fetch_demo.py

# Normalize (gunzip) + stage to S3 + StartFHIRImportJob
uv run scripts/healthlake_import.py --source mimic \
  --input-dir data/mimic-iv-fhir-demo

# Faster quickstart: skip MimicObservationChartevents (35 MB of the 49.5 MB)
uv run scripts/healthlake_import.py --source mimic \
  --input-dir data/mimic-iv-fhir-demo --subset core
```

The data is deliberately not committed: ODbL §4.4 share-alike and §4.6 would attach
to any mirrored copy, making this an ODbL-licensed data repository. Publishing
anything derived from it requires the §4.3 notice:

> Contains information from MIMIC-IV Clinical Database Demo on FHIR, which is made
> available here under the Open Database License (ODbL).

Plus the requested citations, listed in `scripts/mimic/fetch_demo.py`.

The **full** MIMIC-IV on FHIR dataset is credentialed access and must never be
mirrored or have per-record outputs published. Bring your own local copy and point
`--input-dir` at it.

### Curated cohort (optional)

To import only a clinically interesting subset — cheaper and faster:

```bash
# 1. Flag which patients match each scenario (streams NDJSON, no AWS calls)
uv run scripts/mimic/select_mimic_scenarios.py --input-dir data/mimic-iv-fhir-demo

# 2. Pick a seeded, balanced cohort
uv run scripts/mimic/curate_mimic_experiment.py --per-scenario 2 --general 2

# 3. Import only that cohort
uv run scripts/healthlake_import.py --source mimic \
  --input-dir data/mimic-iv-fhir-demo \
  --patients-file data/mimic-cohort.json
```

Filtering happens *before* import, so nothing extra lands in a billed datastore.

### Synthea sample (fallback path)

No PhysioNet dependency at all. The `synthea-sample-data` repo has no tags, so the
artifact is pinned by commit:

```bash
curl -LO https://raw.githubusercontent.com/synthetichealth/synthea-sample-data/a59acd49f50d240ce6bacc22a719fff8b25b8dc3/downloads/synthea_sample_data_fhir_r4_nov2021.zip
unzip -d data/synthea synthea_sample_data_fhir_r4_nov2021.zip

uv run scripts/healthlake_import.py --source synthea \
  --input-dir data/synthea/fhir --limit 20
```

Synthea ships per-patient FHIR *transaction* Bundles, so normalization does more
work here: unwrap `entry.resource`, rewrite `urn:uuid:<id>` references to
`<Type>/<id>`, and resolve conditional references
(`Practitioner?identifier=...`) against the companion `hospitalInformation*` and
`practitionerInformation*` bundles. Keep those companion files in `--input-dir`;
`--limit` caps patient bundles only and always processes them.

### Normalize without importing

```bash
uv run scripts/healthlake_import.py --source mimic \
  --input-dir data/mimic-iv-fhir-demo --normalize-only
```

Writes NDJSON to `data/.healthlake-ndjson/` and makes no AWS calls. Useful for
inspecting the conversion before paying for a datastore.

### What the import back end does

- Uploads one NDJSON file per resource type under `import/<job name>/`
- Waits for a free slot — HealthLake allows **1 concurrent import job per region**
- Runs with `ValidationLevel=structure-only`: MIMIC's custom IG profiles are not
  HealthLake-supported and mass-reject under the default `strict`
- Polls `DescribeFHIRImportJob` every 10s (the API allows 10 req/s)
- Reads the job's **`Manifest.json`** — capital M; the lowercase name every AWS doc
  page prints returns 404
- Treats a missing `FAILURE/` prefix as success: HealthLake does not create the
  prefix when nothing fails
- Ignores `output/.healthlake_write_access_check_file.temp`, the 0-byte write-access
  probe HealthLake writes at submit time

Import is upsert-by-id, so re-running the same import is idempotent and there is no
load-order requirement — references are stored literally and dangling references
import cleanly.

### Verify data landed

```python
patients = client.search("Patient", {"_count": "5", "_total": "accurate"})
conditions = client.search("Condition", {"patient": patients[0]["id"],
                                         "clinical-status": "active"})
```

Search is **eventually consistent** by default, so a resource may not be searchable
the instant an import finishes.

## 3. Query with RestFHIRClient (Python)

```python
from medical_nudging.search.fhir_client import create_healthlake_client

client = create_healthlake_client(endpoint, region="us-east-1")

# Active conditions
conditions = client.search("Condition", {"patient": pid, "clinical-status": "active"})

# Recent labs
labs = client.search("Observation", {
    "patient": pid,
    "category": "laboratory",
    "_sort": "-date",
    "_count": "10",
})

# Active medications
meds = client.search("MedicationRequest", {"patient": pid, "status": "active"})

# Read a single patient
patient = client.read("Patient", pid)
```

Client behaviour worth knowing:

| Behaviour | Detail |
|---|---|
| Auth | SigV4, signed with botocore for service `healthlake`, credentials from the standard chain (auto-refreshing) |
| Search verb | `POST /<Type>/_search` by default, so patient ids stay out of URLs and access logs |
| Pagination | Follows `Bundle.link[next]` verbatim up to `max_pages` (default 10) and logs when it stops at the cap |
| `_count` | Clamped to 1..100 (HealthLake's hard maximum) |
| `_include` | `search.mode == "include"` entries are dropped, so they never masquerade as matches |
| `_total` | Use `accurate` or `none`; `estimate` is invalid on HealthLake |
| String search | Case-sensitive |

### Other FHIR R4 servers

Authentication is injected, so the client is not HealthLake-specific even though
the app only exposes HealthLake config:

```python
from medical_nudging.search.fhir_client import RestFHIRClient

client = RestFHIRClient("https://fhir.example.com/r4", bearer_token="oauth-token")
client = RestFHIRClient("https://fhir.example.com/r4", api_key="key")
client = RestFHIRClient("https://fhir.example.com/r4", auth=my_auth_base)
```

## 4. Query with @tool (agent tool)

```python
from medical_nudging.search.fhir_client import create_healthlake_client
from medical_nudging.tools.fhir_query import create_fhir_query_tool

client = create_healthlake_client(endpoint, region="us-east-1")
tool = create_fhir_query_tool(client)

pid = client.search("Patient", {"_count": "1"})[0]["id"]

# Default query (Condition, MedicationRequest, AllergyIntolerance, Observation)
print(tool(patient_id=str(pid)))

# Targeted query
print(
    tool(
        patient_id=str(pid),
        resource_types=["Patient", "Condition", "Observation"],
        params={"_count": "5"},
    )
)
```

### Example output

```
### Patient (1 results)
- America446 Steuber698, female, DOB: 1943-05-15

### Condition (2 results)
- Body mass index 30+ - obesity (finding) (status: active) onset: 1977-07-30
- Osteoarthritis of knee (status: active) onset: 1986-05-04

### Observation (5 results)
- Low Density Lipoprotein Cholesterol: 74.98 mg/dL (2017-09-30)
- Triglycerides: 119.22 mg/dL (2017-09-30)
```

## 5. Run FHIR API inference

With `fhir_api.enabled: true` and a datastore endpoint configured, the orchestrator
queries HealthLake on demand instead of receiving a document string.

```bash
# Auto-discover patients from the datastore and run inference
uv run scripts/fhir_api/run_inference.py -v

# With an experiment config
uv run scripts/fhir_api/run_inference.py --exp-config config/exps/fhir_api_baseline.yaml

# List discovered patients without invoking the agent
uv run scripts/fhir_api/run_inference.py --dry-run
```

### Using the orchestrator directly

```python
from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator

orchestrator = MedicalNudgingOrchestrator(
    specialty="general",
    model_id="us.anthropic.claude-sonnet-5",
)

response = orchestrator.generate_nudges(
    visit_context={
        "data_source": "fhir_api",
        "patient_id": "28dcf33b-0c52-587f-83ad-2a3270976719",
        "specialty": "general",
        "visit_type": "ambulatory",
    }
)
```

### Data source modes

| `data_source` | Description | `patient_data` param |
|---|---|---|
| `"file"` or `None` | Document string (CCDA XML, FHIR JSON, pre-parsed JSON) — zero cost | Required |
| `"fhir_api"` | Agent queries HealthLake on demand | `None` (not needed) |

When `data_source="fhir_api"`:
- `patient_id` is required in `visit_context`
- `query_patient_fhir` is added to the agent's tool list
- `get_patient_data` is excluded (no document to parse)
- If both `patient_data` and `data_source="fhir_api"` are provided, the document wins

## Configuration

### settings.yaml

```yaml
fhir_api:
  enabled: true
  datastore_endpoint: "https://healthlake.us-east-1.amazonaws.com/datastore/<id>/r4/"
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `FHIR_API_ENABLED` | No | Enable the HealthLake data source (`true`/`false`) |
| `HEALTHLAKE_DATASTORE_ENDPOINT` | When enabled | Datastore endpoint; `FHIR_API_BASE_URL` is accepted as a legacy alias |
| `AWS_REGION` | No | Region used for SigV4 signing (default `us-east-1`) |

There is no API key: authorization is IAM. The caller needs
`healthlake:ReadResource`, `healthlake:SearchWithGet`, `healthlake:SearchWithPost`,
and `healthlake:GetCapabilities` on the datastore ARN.

## Troubleshooting

**Datastore stuck in `CREATING`** — allow up to 30 minutes (2 to 3 is typical). If it never becomes
`ACTIVE`, check for a Service Control Policy restricting `healthlake:*`, and note
that the docs' Lake Formation data-lake-admin prerequisite applies to the SQL
index/query integration (a plain R4 datastore did not require it in testing).

**`AccessDeniedException` on FHIR calls** — the caller's IAM policy is missing a
per-interaction action (each FHIR interaction has its own, e.g.
`healthlake:SearchWithPost`). Verify with `client.metadata()`, which only needs
`healthlake:GetCapabilities`.

**Import job fails immediately** — `StartFHIRImportJob` needs `iam:PassRole` for the
import role, and a freshly created role can take a few seconds to become assumable
(the pipeline retries `AccessDeniedException` for exactly this reason).

**Import completes with errors** — read `Manifest.json` (capital M) and the
`FAILURE/` NDJSON under
`s3://<staging bucket>/output/<datastoreId>-FHIR_IMPORT-<jobId>/`. Each failure line
carries `lineId`, an `OperationOutcome`, and an HTTP status code.

**Search returns nothing right after an import** — search is eventually consistent.
Retry, or use the `x-amz-fhir-history-consistency-level` header for strong
consistency.

**`_total=estimate` rejected / `_count>100` rejected** — HealthLake supports only
`accurate` and `none`, and caps page size at 100. The client clamps `_count`.

## Files reference

| File | Purpose |
|---|---|
| `terraform/healthlake.tf` | Datastore, KMS key, staging bucket, import role, budget |
| `scripts/mimic/fetch_demo.py` | Download the open-access MIMIC-IV-on-FHIR demo |
| `scripts/healthlake_import.py` | Normalize → S3 → `StartFHIRImportJob` CLI |
| `src/medical_nudging/healthlake/normalize.py` | Per-source NDJSON normalization |
| `src/medical_nudging/healthlake/import_job.py` | Staging, job control, manifest parsing |
| `scripts/mimic/select_mimic_scenarios.py` | Flag scenario-matching patients from NDJSON |
| `scripts/mimic/curate_mimic_experiment.py` | Seeded balanced cohort file |
| `src/medical_nudging/search/fhir_client.py` | `RestFHIRClient` + `create_healthlake_client` |
| `src/medical_nudging/search/fhir_auth.py` | SigV4 `requests` auth |
| `src/medical_nudging/tools/fhir_query.py` | `@tool query_patient_fhir` |
| `docs/fhir-api-integration.md` | Design document and trade-offs |
