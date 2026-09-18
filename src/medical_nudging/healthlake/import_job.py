"""Stage NDJSON in S3 and run a HealthLake ``StartFHIRImportJob``.

Empirically-derived details this module encodes (each one is a silent-failure
trap otherwise):

* The job report object is ``Manifest.json`` — **capital M**. Every AWS doc page
  calls it ``manifest.json``; that key returns 404.
* A missing ``FAILURE/`` prefix means success. HealthLake does not create the
  prefix when there are no failures, so absence must not be read as an error.
* HealthLake writes ``output/.healthlake_write_access_check_file.temp`` (0 bytes)
  at submit time to pre-flight write access. Ignore it.
* ``ValidationLevel`` must be ``structure-only``: MIMIC-IV's custom IG profiles
  are not HealthLake-supported and mass-reject under the default ``strict``.
* ``JobOutputDataConfig.S3Configuration`` **requires** ``KmsKeyId``.
* Only **1 concurrent import job per region** — jobs must be serialized. This
  module waits for a free slot rather than relying on server-side queuing.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

ValidationLevel = Literal["strict", "structure-only", "minimal"]

# MIMIC's custom IG profiles are not HealthLake-supported; strict validation
# rejects them en masse. structure-only also safely ignores Synthea's US Core
# meta.profile claims, so one setting covers both sources.
DEFAULT_VALIDATION_LEVEL: ValidationLevel = "structure-only"

MANIFEST_OBJECT_NAME = "Manifest.json"
FAILURE_PREFIX = "FAILURE/"
SUCCESS_PREFIX = "SUCCESS/"
WRITE_ACCESS_CHECK_SUFFIX = ".healthlake_write_access_check_file.temp"

TERMINAL_STATUSES = frozenset(
    {
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
        "FAILED",
        "CANCEL_COMPLETED",
        "CANCEL_FAILED",
    }
)
BUSY_STATUSES = frozenset({"SUBMITTED", "QUEUED", "IN_PROGRESS"})

# DescribeFHIRImportJob is capped at 10 requests/second; 10s polling is ample.
POLL_INTERVAL_SECONDS = 10
DEFAULT_JOB_TIMEOUT_SECONDS = 3600

# IAM is eventually consistent; a freshly created import role can take a few
# seconds to be assumable by the service.
IAM_PROPAGATION_ATTEMPTS = 6
IAM_PROPAGATION_SLEEP_SECONDS = 10


class ImportJobError(Exception):
    """Raised when an import job cannot be started or does not complete."""


@dataclass
class ImportManifest:
    """Parsed ``Manifest.json`` counters from a completed import job."""

    files_scanned: int = 0
    files_imported: int = 0
    resources_scanned: int = 0
    resources_imported: int = 0
    resources_with_customer_error: int = 0
    resources_with_server_error: int = 0
    raw: dict[str, Any] | None = None

    @property
    def clean(self) -> bool:
        """True when every scanned resource imported with no errors."""
        return (
            self.resources_with_customer_error == 0
            and self.resources_with_server_error == 0
            and self.resources_scanned == self.resources_imported
        )

    def summary(self) -> str:
        return (
            f"{self.resources_imported}/{self.resources_scanned} resources imported from "
            f"{self.files_imported}/{self.files_scanned} files; "
            f"customer errors={self.resources_with_customer_error}, "
            f"server errors={self.resources_with_server_error}"
        )


@dataclass
class ImportResult:
    """Outcome of one import job."""

    job_id: str
    job_status: str
    staged_objects: int
    output_prefix: str
    manifest: ImportManifest | None = None
    failure_objects: list[str] | None = None

    @property
    def succeeded(self) -> bool:
        if self.job_status != "COMPLETED":
            return False
        if self.failure_objects:
            return False
        return self.manifest is None or self.manifest.clean


def stage_ndjson(
    s3_client: Any,
    ndjson_dir: Path,
    bucket: str,
    prefix: str,
) -> int:
    """Upload every ``.ndjson`` file in a directory under an S3 prefix.

    Args:
        s3_client: A boto3 S3 client.
        ndjson_dir: Local directory of ``.ndjson`` files.
        bucket: Destination bucket.
        prefix: Destination key prefix (e.g. ``"import/mimic"``).

    Returns:
        Number of objects uploaded.

    Raises:
        FileNotFoundError: If the directory holds no NDJSON.
        ImportJobError: If an upload fails.
    """
    files = sorted(ndjson_dir.glob("*.ndjson"))
    if not files:
        raise FileNotFoundError(f"No .ndjson files to stage under {ndjson_dir}")

    normalized_prefix = prefix.strip("/")
    for path in files:
        key = f"{normalized_prefix}/{path.name}"
        logger.info(f"Staging s3://{bucket}/{key} ({path.stat().st_size:,} bytes)")
        try:
            s3_client.upload_file(str(path), bucket, key)
        except (ClientError, OSError) as e:
            raise ImportJobError(f"Failed to stage {path.name} to s3://{bucket}/{key}: {e}") from e
    return len(files)


def wait_for_import_slot(
    healthlake_client: Any,
    datastore_id: str,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    sleep: Any = time.sleep,
) -> None:
    """Block until no import job is in flight for the datastore.

    HealthLake allows only one concurrent import job per region, so jobs are
    serialized here rather than left to server-side queuing.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        active = _active_jobs(healthlake_client, datastore_id)
        if not active:
            return
        if time.monotonic() >= deadline:
            raise ImportJobError(
                f"Import jobs still running after {timeout_seconds}s: {', '.join(active)}"
            )
        logger.info(f"Waiting for in-flight import job(s) to finish: {', '.join(active)}")
        sleep(poll_interval_seconds)


def _active_jobs(healthlake_client: Any, datastore_id: str) -> list[str]:
    try:
        response = healthlake_client.list_fhir_import_jobs(DatastoreId=datastore_id)
    except ClientError as e:
        raise ImportJobError(f"ListFHIRImportJobs failed for {datastore_id}: {e}") from e

    jobs = response.get("ImportJobPropertiesList", [])
    return [
        job["JobId"]
        for job in jobs
        if isinstance(job, dict) and job.get("JobStatus") in BUSY_STATUSES and "JobId" in job
    ]


def start_import_job(
    healthlake_client: Any,
    datastore_id: str,
    job_name: str,
    input_s3_uri: str,
    output_s3_uri: str,
    kms_key_arn: str,
    data_access_role_arn: str,
    validation_level: ValidationLevel = DEFAULT_VALIDATION_LEVEL,
    sleep: Any = time.sleep,
) -> str:
    """Start an import job, retrying past IAM eventual consistency.

    Returns:
        The job id.

    Raises:
        ImportJobError: If the job cannot be started.
    """
    request = {
        "JobName": job_name,
        "DatastoreId": datastore_id,
        "InputDataConfig": {"S3Uri": input_s3_uri},
        "JobOutputDataConfig": {
            "S3Configuration": {
                "S3Uri": output_s3_uri,
                # Not optional: the API rejects the request without a key.
                "KmsKeyId": kms_key_arn,
            }
        },
        "DataAccessRoleArn": data_access_role_arn,
        "ValidationLevel": validation_level,
    }

    last_error: ClientError | None = None
    for attempt in range(1, IAM_PROPAGATION_ATTEMPTS + 1):
        try:
            response = healthlake_client.start_fhir_import_job(**request)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in ("AccessDeniedException", "ValidationException"):
                raise ImportJobError(f"StartFHIRImportJob failed: {e}") from e
            last_error = e
            logger.warning(
                f"StartFHIRImportJob attempt {attempt}/{IAM_PROPAGATION_ATTEMPTS} rejected "
                f"({code}) — likely IAM propagation, retrying"
            )
            sleep(IAM_PROPAGATION_SLEEP_SECONDS)
            continue

        job_id = response.get("JobId")
        if not isinstance(job_id, str) or not job_id:
            raise ImportJobError(f"StartFHIRImportJob returned no JobId: {response}")
        logger.info(f"Started import job {job_id} ({validation_level}) from {input_s3_uri}")
        return job_id

    raise ImportJobError(
        f"StartFHIRImportJob still failing after {IAM_PROPAGATION_ATTEMPTS} attempts: {last_error}"
    )


def wait_for_import_job(
    healthlake_client: Any,
    datastore_id: str,
    job_id: str,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Poll ``DescribeFHIRImportJob`` until the job reaches a terminal status.

    Returns:
        The terminal ``ImportJobProperties`` dict.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            response = healthlake_client.describe_fhir_import_job(
                DatastoreId=datastore_id, JobId=job_id
            )
        except ClientError as e:
            raise ImportJobError(f"DescribeFHIRImportJob failed for {job_id}: {e}") from e

        properties = response.get("ImportJobProperties")
        if not isinstance(properties, dict):
            raise ImportJobError(f"DescribeFHIRImportJob returned no properties for {job_id}")

        status = properties.get("JobStatus", "UNKNOWN")
        if status in TERMINAL_STATUSES:
            logger.info(f"Import job {job_id} finished with status {status}")
            return properties

        if time.monotonic() >= deadline:
            raise ImportJobError(
                f"Import job {job_id} still {status} after {timeout_seconds}s — "
                "poll DescribeFHIRImportJob manually"
            )
        logger.info(f"Import job {job_id}: {status}")
        sleep(poll_interval_seconds)


def parse_manifest(s3_client: Any, bucket: str, output_prefix: str) -> ImportManifest | None:
    """Read the job's ``Manifest.json``.

    Returns ``None`` when the object is absent (which happens if the job failed
    before writing a report).
    """
    key = f"{output_prefix.rstrip('/')}/{MANIFEST_OBJECT_NAME}"
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            logger.warning(f"No {MANIFEST_OBJECT_NAME} at s3://{bucket}/{key}")
            return None
        raise ImportJobError(f"Cannot read s3://{bucket}/{key}: {e}") from e

    body = response["Body"].read()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise ImportJobError(f"s3://{bucket}/{key} is not valid JSON: {e}") from e

    if not isinstance(payload, dict):
        raise ImportJobError(f"s3://{bucket}/{key} is not a JSON object")

    return ImportManifest(
        files_scanned=_as_int(payload.get("numberOfScannedFiles")),
        files_imported=_as_int(payload.get("numberOfFilesImported")),
        resources_scanned=_as_int(payload.get("numberOfResourcesScanned")),
        resources_imported=_as_int(payload.get("numberOfResourcesImportedSuccessfully")),
        resources_with_customer_error=_as_int(payload.get("numberOfResourcesWithCustomerError")),
        resources_with_server_error=_as_int(payload.get("numberOfResourcesWithServerError")),
        raw=payload,
    )


def list_failure_objects(s3_client: Any, bucket: str, output_prefix: str) -> list[str]:
    """List the job's ``FAILURE/`` objects.

    An empty list means the job had no failures: HealthLake does not create the
    ``FAILURE/`` prefix at all when nothing failed, so a missing prefix is
    success, not an error. The 0-byte write-access probe object is ignored.
    """
    prefix = f"{output_prefix.rstrip('/')}/{FAILURE_PREFIX}"
    keys: list[str] = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if key.endswith(WRITE_ACCESS_CHECK_SUFFIX) or key.endswith("/"):
                    continue
                keys.append(key)
    except ClientError as e:
        raise ImportJobError(f"Cannot list s3://{bucket}/{prefix}: {e}") from e
    return keys


def output_prefix_for_job(datastore_id: str, job_id: str, output_prefix: str) -> str:
    """Build the job's output prefix.

    HealthLake writes under ``<output prefix>/<datastoreId>-FHIR_IMPORT-<jobId>/``
    using the real ids, not the hashes the docs imply.
    """
    return f"{output_prefix.strip('/')}/{datastore_id}-FHIR_IMPORT-{job_id}"


def run_import(
    healthlake_client: Any,
    s3_client: Any,
    datastore_id: str,
    ndjson_dir: Path,
    bucket: str,
    job_name: str,
    data_access_role_arn: str,
    kms_key_arn: str,
    input_prefix: str = "import",
    output_prefix: str = "output",
    validation_level: ValidationLevel = DEFAULT_VALIDATION_LEVEL,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    sleep: Any = time.sleep,
) -> ImportResult:
    """Stage NDJSON, run one import job, and read its report.

    Args:
        healthlake_client: A boto3 ``healthlake`` client.
        s3_client: A boto3 ``s3`` client.
        datastore_id: Target datastore id.
        ndjson_dir: Local directory of normalized ``.ndjson`` files.
        bucket: Staging bucket (terraform output ``healthlake_staging_bucket``).
        job_name: Import job name, also used as the input sub-prefix.
        data_access_role_arn: Role HealthLake assumes (``healthlake_import_role_arn``).
        kms_key_arn: Key for the job output (``healthlake_import_kms_key_arn``).
        input_prefix: Base prefix for staged NDJSON.
        output_prefix: Base prefix for job output.
        validation_level: Leave at ``structure-only`` unless you know otherwise.
        timeout_seconds: Cap on both the slot wait and the job wait.
        poll_interval_seconds: Polling interval.
        sleep: Injected sleep, for tests.

    Returns:
        An :class:`ImportResult`. Check ``.succeeded``.
    """
    job_input_prefix = f"{input_prefix.strip('/')}/{job_name}"
    staged = stage_ndjson(s3_client, ndjson_dir, bucket, job_input_prefix)

    wait_for_import_slot(
        healthlake_client,
        datastore_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )

    job_id = start_import_job(
        healthlake_client,
        datastore_id=datastore_id,
        job_name=job_name,
        input_s3_uri=f"s3://{bucket}/{job_input_prefix}/",
        output_s3_uri=f"s3://{bucket}/{output_prefix.strip('/')}/",
        kms_key_arn=kms_key_arn,
        data_access_role_arn=data_access_role_arn,
        validation_level=validation_level,
        sleep=sleep,
    )

    properties = wait_for_import_job(
        healthlake_client,
        datastore_id,
        job_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )
    status = properties.get("JobStatus", "UNKNOWN")

    job_output_prefix = output_prefix_for_job(datastore_id, job_id, output_prefix)
    manifest = parse_manifest(s3_client, bucket, job_output_prefix)
    failures = list_failure_objects(s3_client, bucket, job_output_prefix)

    result = ImportResult(
        job_id=job_id,
        job_status=status,
        staged_objects=staged,
        output_prefix=job_output_prefix,
        manifest=manifest,
        failure_objects=failures,
    )

    if manifest is not None:
        logger.info(f"Import {job_id}: {manifest.summary()}")
    if failures:
        logger.error(f"Import {job_id}: {len(failures)} FAILURE/ object(s) — inspect them in S3")
    return result


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
