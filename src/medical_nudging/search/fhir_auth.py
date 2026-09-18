"""Pluggable authentication for :class:`~medical_nudging.search.fhir_client.RestFHIRClient`.

The FHIR client itself stays transport-generic: it knows how to speak FHIR R4
over HTTP and delegates credentials to an injected ``requests.auth.AuthBase``.
This module provides the AWS HealthLake implementation of that seam — SigV4
request signing via botocore.

Usage:
    from medical_nudging.search.fhir_auth import create_healthlake_auth

    auth = create_healthlake_auth(region="us-east-1")
    client = RestFHIRClient(datastore_endpoint, auth=auth, search_method="POST")

Notes:
    * The signing service name is ``healthlake`` (IAM/SigV4 service prefix).
    * Credentials come from ``boto3.Session().get_credentials()``, which
      auto-refreshes SSO / assume-role credentials — do not snapshot frozen keys.
    * ``requests`` prepares the URL (``prepare_url``) *before* ``prepare_auth``,
      so the auth callable sees the fully-assembled query string and SigV4 signs
      it. ``tests/test_fhir_auth.py`` pins that ordering.
"""

import logging
from typing import Any, Literal

import requests.auth
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

logger = logging.getLogger(__name__)


class FHIRAuthError(Exception):
    """Raised when FHIR authentication cannot be configured."""


# Signing service name for AWS HealthLake's FHIR REST endpoints.
HEALTHLAKE_SERVICE_NAME: Literal["healthlake"] = "healthlake"

# Headers that carry a previous signature. They are stripped before re-signing
# so that signing the same PreparedRequest twice (redirects, retries, a
# next-link fetch) cannot fold a stale signature into the canonical request.
_SIGNATURE_HEADERS = ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256")

# Connection-management headers must be excluded from the canonical request.
# `requests` sets `Connection: keep-alive` by default, but urllib3 owns the
# connection lifecycle and rewrites or drops these on the wire — so a signature
# computed over them will not match what the service receives, and HealthLake
# rejects the request with "The request signature we calculated does not match".
# SigV4 permits extra *unsigned* headers, so dropping them from the canonical
# request is sufficient; they still go out on the wire.
_UNSIGNED_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
    }
)


class SigV4RequestsAuth(requests.auth.AuthBase):
    """Sign ``requests`` calls with AWS SigV4.

    Args:
        credentials: A botocore credential provider. Pass the object returned by
            ``boto3.Session().get_credentials()`` (a ``RefreshableCredentials``
            in the SSO / assume-role case) so signing picks up rotated keys.
        service_name: SigV4 service name (``"healthlake"`` for HealthLake).
        region: AWS region of the endpoint being called.
    """

    def __init__(self, credentials: Credentials, service_name: str, region: str):
        self.credentials = credentials
        self.service_name = service_name
        self.region = region

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        for header in _SIGNATURE_HEADERS:
            request.headers.pop(header, None)

        # request.url is already the final URL including the query string, and
        # request.body is already the exact bytes that will go on the wire.
        signable_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _UNSIGNED_HEADERS
        }
        aws_request = AWSRequest(
            method=request.method,
            url=request.url,
            data=request.body,
            headers=signable_headers,
        )
        SigV4Auth(self.credentials, self.service_name, self.region).add_auth(aws_request)

        for header, value in aws_request.headers.items():
            request.headers[header] = value
        return request


def create_healthlake_auth(
    region: str,
    boto_session: Any = None,
) -> SigV4RequestsAuth:
    """Build a SigV4 auth object for AWS HealthLake.

    Args:
        region: AWS region hosting the datastore (e.g. ``"us-east-1"``).
        boto_session: Optional ``boto3.Session``. A default session is created
            when omitted, which resolves credentials from the standard chain.

    Returns:
        A ``requests``-compatible auth object.

    Raises:
        FHIRAuthError: If no AWS credentials can be resolved.
    """
    if boto_session is None:
        import boto3

        boto_session = boto3.Session(region_name=region)

    credentials = boto_session.get_credentials()
    if credentials is None:
        raise FHIRAuthError(
            "No AWS credentials found for HealthLake SigV4 signing. "
            "Configure AWS_PROFILE or the standard credential chain."
        )

    logger.debug(f"HealthLake SigV4 auth configured for region {region}")
    return SigV4RequestsAuth(credentials, HEALTHLAKE_SERVICE_NAME, region)
