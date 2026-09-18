"""Tests for the HealthLake import-job back end.

The assertions here pin the empirically-discovered quirks: ``Manifest.json`` with
a capital M, a missing ``FAILURE/`` prefix meaning success, the write-access probe
object being ignored, ``structure-only`` validation, and the required
``KmsKeyId``.
"""

import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from medical_nudging.healthlake.import_job import (
    DEFAULT_VALIDATION_LEVEL,
    MANIFEST_OBJECT_NAME,
    WRITE_ACCESS_CHECK_SUFFIX,
    ImportJobError,
    list_failure_objects,
    output_prefix_for_job,
    parse_manifest,
    run_import,
    stage_ndjson,
    start_import_job,
    wait_for_import_job,
    wait_for_import_slot,
)

DATASTORE_ID = "d1234567890abcdef"
JOB_ID = "job-abc"
BUCKET = "medical-nudging-healthlake-123456789012"
ROLE_ARN = "arn:aws:iam::123456789012:role/medical-nudging-healthlake-import"
KMS_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"

CLEAN_MANIFEST = {
    "numberOfScannedFiles": 16,
    "numberOfFilesImported": 16,
    "sizeOfScannedFilesInMB": 2.36,
    "sizeOfDataImportedSuccessfullyInMB": 2.36,
    "numberOfResourcesScanned": 1301,
    "numberOfResourcesImportedSuccessfully": 1301,
    "numberOfResourcesWithCustomerError": 0,
    "numberOfResourcesWithServerError": 0,
}


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeS3:
    """Minimal S3 stand-in: records uploads, serves objects, paginates listings."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.uploads: list[tuple[str, str]] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.uploads.append((filename, key))
        self.objects[key] = Path(filename).read_bytes()

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 signature
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")

        class _Body:
            def __init__(self, payload: bytes):
                self._payload = payload

            def read(self) -> bytes:
                return self._payload

        return {"Body": _Body(self.objects[Key])}

    def get_paginator(self, name: str):
        objects = self.objects

        class _Paginator:
            def paginate(self, Bucket: str, Prefix: str):  # noqa: N803 - boto3 signature
                contents = [{"Key": key} for key in sorted(objects) if key.startswith(Prefix)]
                yield {"Contents": contents}

        return _Paginator()


class FakeHealthLake:
    """Minimal HealthLake stand-in with scripted job statuses."""

    def __init__(
        self,
        statuses: list[str] | None = None,
        active_jobs: list[list[str]] | None = None,
        start_errors: list[ClientError] | None = None,
    ):
        self.statuses = statuses or ["COMPLETED"]
        self.active_jobs = active_jobs or [[]]
        self.start_errors = start_errors or []
        self.start_requests: list[dict] = []
        self.describe_calls = 0
        self.list_calls = 0

    def list_fhir_import_jobs(self, DatastoreId: str) -> dict:  # noqa: N803
        index = min(self.list_calls, len(self.active_jobs) - 1)
        self.list_calls += 1
        return {
            "ImportJobPropertiesList": [
                {"JobId": job_id, "JobStatus": "IN_PROGRESS"} for job_id in self.active_jobs[index]
            ]
        }

    def start_fhir_import_job(self, **kwargs) -> dict:
        self.start_requests.append(kwargs)
        if self.start_errors:
            raise self.start_errors.pop(0)
        return {"JobId": JOB_ID, "JobStatus": "SUBMITTED"}

    def describe_fhir_import_job(self, DatastoreId: str, JobId: str) -> dict:  # noqa: N803
        index = min(self.describe_calls, len(self.statuses) - 1)
        self.describe_calls += 1
        return {"ImportJobProperties": {"JobId": JobId, "JobStatus": self.statuses[index]}}


@pytest.fixture
def ndjson_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "ndjson"
    directory.mkdir()
    (directory / "MimicPatient.ndjson").write_text('{"resourceType":"Patient","id":"p1"}\n')
    (directory / "MimicCondition.ndjson").write_text('{"resourceType":"Condition","id":"c1"}\n')
    (directory / "notes.txt").write_text("ignored")
    return directory


class TestStaging:
    def test_only_ndjson_is_uploaded(self, ndjson_dir: Path):
        s3 = FakeS3()
        count = stage_ndjson(s3, ndjson_dir, BUCKET, "import/mimic")
        assert count == 2
        assert sorted(key for _, key in s3.uploads) == [
            "import/mimic/MimicCondition.ndjson",
            "import/mimic/MimicPatient.ndjson",
        ]

    def test_empty_directory_raises(self, tmp_path: Path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(FileNotFoundError):
            stage_ndjson(FakeS3(), tmp_path / "empty", BUCKET, "import")

    def test_upload_failure_is_wrapped(self, ndjson_dir: Path):
        class Failing(FakeS3):
            def upload_file(self, filename, bucket, key):
                raise _client_error("AccessDenied", "PutObject")

        with pytest.raises(ImportJobError, match="Failed to stage"):
            stage_ndjson(Failing(), ndjson_dir, BUCKET, "import")


class TestStartJob:
    def test_request_shape(self):
        healthlake = FakeHealthLake()
        job_id = start_import_job(
            healthlake,
            datastore_id=DATASTORE_ID,
            job_name="mimic-1",
            input_s3_uri=f"s3://{BUCKET}/import/mimic-1/",
            output_s3_uri=f"s3://{BUCKET}/output/",
            kms_key_arn=KMS_KEY_ARN,
            data_access_role_arn=ROLE_ARN,
            sleep=lambda _: None,
        )
        assert job_id == JOB_ID

        request = healthlake.start_requests[0]
        assert request["ValidationLevel"] == "structure-only" == DEFAULT_VALIDATION_LEVEL
        assert request["JobOutputDataConfig"]["S3Configuration"]["KmsKeyId"] == KMS_KEY_ARN
        assert request["InputDataConfig"]["S3Uri"].endswith("/import/mimic-1/")
        assert request["DataAccessRoleArn"] == ROLE_ARN

    def test_access_denied_is_retried_for_iam_propagation(self):
        healthlake = FakeHealthLake(
            start_errors=[_client_error("AccessDeniedException", "StartFHIRImportJob")]
        )
        job_id = start_import_job(
            healthlake,
            datastore_id=DATASTORE_ID,
            job_name="mimic-1",
            input_s3_uri=f"s3://{BUCKET}/import/mimic-1/",
            output_s3_uri=f"s3://{BUCKET}/output/",
            kms_key_arn=KMS_KEY_ARN,
            data_access_role_arn=ROLE_ARN,
            sleep=lambda _: None,
        )
        assert job_id == JOB_ID
        assert len(healthlake.start_requests) == 2

    def test_other_errors_are_not_retried(self):
        healthlake = FakeHealthLake(
            start_errors=[_client_error("ResourceNotFoundException", "StartFHIRImportJob")]
        )
        with pytest.raises(ImportJobError, match="StartFHIRImportJob failed"):
            start_import_job(
                healthlake,
                datastore_id=DATASTORE_ID,
                job_name="mimic-1",
                input_s3_uri=f"s3://{BUCKET}/import/mimic-1/",
                output_s3_uri=f"s3://{BUCKET}/output/",
                kms_key_arn=KMS_KEY_ARN,
                data_access_role_arn=ROLE_ARN,
                sleep=lambda _: None,
            )


class TestSerialization:
    def test_waits_for_the_single_concurrent_slot(self):
        healthlake = FakeHealthLake(active_jobs=[["other-job"], ["other-job"], []])
        slept: list[int] = []
        wait_for_import_slot(healthlake, DATASTORE_ID, sleep=slept.append)
        assert healthlake.list_calls == 3
        assert slept == [10, 10]

    def test_returns_immediately_when_idle(self):
        healthlake = FakeHealthLake(active_jobs=[[]])
        wait_for_import_slot(healthlake, DATASTORE_ID, sleep=lambda _: None)
        assert healthlake.list_calls == 1


class TestPolling:
    def test_polls_until_terminal(self):
        healthlake = FakeHealthLake(statuses=["SUBMITTED", "IN_PROGRESS", "COMPLETED"])
        properties = wait_for_import_job(healthlake, DATASTORE_ID, JOB_ID, sleep=lambda _: None)
        assert properties["JobStatus"] == "COMPLETED"
        assert healthlake.describe_calls == 3

    def test_completed_with_errors_is_terminal(self):
        healthlake = FakeHealthLake(statuses=["COMPLETED_WITH_ERRORS"])
        properties = wait_for_import_job(healthlake, DATASTORE_ID, JOB_ID, sleep=lambda _: None)
        assert properties["JobStatus"] == "COMPLETED_WITH_ERRORS"


class TestManifest:
    def test_manifest_object_name_is_capital_m(self):
        """AWS docs say manifest.json; the real object is Manifest.json and lowercase 404s."""
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3({f"{prefix}/{MANIFEST_OBJECT_NAME}": json.dumps(CLEAN_MANIFEST).encode()})

        manifest = parse_manifest(s3, BUCKET, prefix)
        assert manifest is not None
        assert manifest.resources_imported == 1301
        assert manifest.clean

    def test_lowercase_manifest_is_not_found(self):
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3({f"{prefix}/manifest.json": json.dumps(CLEAN_MANIFEST).encode()})
        assert parse_manifest(s3, BUCKET, prefix) is None

    def test_partial_import_is_not_clean(self):
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        payload = dict(CLEAN_MANIFEST)
        payload["numberOfResourcesImportedSuccessfully"] = 1300
        payload["numberOfResourcesWithCustomerError"] = 1
        s3 = FakeS3({f"{prefix}/{MANIFEST_OBJECT_NAME}": json.dumps(payload).encode()})

        manifest = parse_manifest(s3, BUCKET, prefix)
        assert manifest is not None
        assert not manifest.clean

    def test_invalid_json_raises(self):
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3({f"{prefix}/{MANIFEST_OBJECT_NAME}": b"{not json}"})
        with pytest.raises(ImportJobError, match="not valid JSON"):
            parse_manifest(s3, BUCKET, prefix)

    def test_output_prefix_uses_real_ids(self):
        assert (
            output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
            == f"output/{DATASTORE_ID}-FHIR_IMPORT-{JOB_ID}"
        )


class TestFailurePrefix:
    def test_missing_failure_prefix_means_success(self):
        """HealthLake does not create FAILURE/ at all when nothing failed."""
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3({f"{prefix}/SUCCESS/MimicPatient.ndjson": b"{}"})
        assert list_failure_objects(s3, BUCKET, prefix) == []

    def test_write_access_probe_object_is_ignored(self):
        """HealthLake writes a 0-byte probe object at submit time; it is not a failure."""
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3(
            {
                f"{prefix}/FAILURE/{WRITE_ACCESS_CHECK_SUFFIX}": b"",
                f"{prefix}/FAILURE/": b"",
            }
        )
        assert list_failure_objects(s3, BUCKET, prefix) == []

    def test_failure_objects_are_reported(self):
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3({f"{prefix}/FAILURE/MimicCondition.ndjson": b'{"lineId":9}'})
        assert list_failure_objects(s3, BUCKET, prefix) == [
            f"{prefix}/FAILURE/MimicCondition.ndjson"
        ]


class TestRunImport:
    def test_end_to_end_clean_run(self, ndjson_dir: Path):
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3({f"{prefix}/{MANIFEST_OBJECT_NAME}": json.dumps(CLEAN_MANIFEST).encode()})
        healthlake = FakeHealthLake(statuses=["IN_PROGRESS", "COMPLETED"])

        result = run_import(
            healthlake_client=healthlake,
            s3_client=s3,
            datastore_id=DATASTORE_ID,
            ndjson_dir=ndjson_dir,
            bucket=BUCKET,
            job_name="mimic-1",
            data_access_role_arn=ROLE_ARN,
            kms_key_arn=KMS_KEY_ARN,
            sleep=lambda _: None,
        )

        assert result.succeeded
        assert result.staged_objects == 2
        assert result.output_prefix == prefix
        assert result.manifest is not None and result.manifest.clean

    def test_failure_objects_make_the_run_unsuccessful(self, ndjson_dir: Path):
        prefix = output_prefix_for_job(DATASTORE_ID, JOB_ID, "output")
        s3 = FakeS3(
            {
                f"{prefix}/{MANIFEST_OBJECT_NAME}": json.dumps(CLEAN_MANIFEST).encode(),
                f"{prefix}/FAILURE/MimicCondition.ndjson": b'{"lineId":9}',
            }
        )
        result = run_import(
            healthlake_client=FakeHealthLake(),
            s3_client=s3,
            datastore_id=DATASTORE_ID,
            ndjson_dir=ndjson_dir,
            bucket=BUCKET,
            job_name="mimic-1",
            data_access_role_arn=ROLE_ARN,
            kms_key_arn=KMS_KEY_ARN,
            sleep=lambda _: None,
        )
        assert not result.succeeded

    def test_completed_with_errors_is_not_success(self, ndjson_dir: Path):
        result = run_import(
            healthlake_client=FakeHealthLake(statuses=["COMPLETED_WITH_ERRORS"]),
            s3_client=FakeS3(),
            datastore_id=DATASTORE_ID,
            ndjson_dir=ndjson_dir,
            bucket=BUCKET,
            job_name="mimic-1",
            data_access_role_arn=ROLE_ARN,
            kms_key_arn=KMS_KEY_ARN,
            sleep=lambda _: None,
        )
        assert result.job_status == "COMPLETED_WITH_ERRORS"
        assert not result.succeeded
