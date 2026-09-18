"""AWS HealthLake bulk-import helpers.

Two stages, shared by every data source:

1. ``normalize`` — turn a source dataset into plain NDJSON on local disk
   (Synthea: unwrap transaction bundles and rewrite references;
   MIMIC-IV on FHIR: gunzip, since it is already NDJSON per resource type).
2. ``import_job`` — stage the NDJSON in S3, run ``StartFHIRImportJob``, poll it,
   and read the job's ``Manifest.json`` report.
"""

from medical_nudging.healthlake.import_job import (
    ImportManifest,
    ImportResult,
    run_import,
)
from medical_nudging.healthlake.normalize import (
    NormalizeStats,
    normalize_mimic,
    normalize_synthea,
)

__all__ = [
    "ImportManifest",
    "ImportResult",
    "NormalizeStats",
    "normalize_mimic",
    "normalize_synthea",
    "run_import",
]
