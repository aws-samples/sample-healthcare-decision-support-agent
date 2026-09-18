"""Tests for SigV4 signing of FHIR requests and searchset pagination.

Two of these tests pin behaviours the HealthLake feasibility spike flagged as
unverified and most likely to break silently:

1. ``requests`` must run ``prepare_url`` before ``prepare_auth``, so the auth
   callable sees the query string and SigV4 signs it (``test_query_string_*``).
2. Next-link pagination must re-sign the returned URL, percent-encoding its query
   string first so the signature matches what HealthLake canonicalizes
   (``test_next_link_*``).
"""

import json
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest
import requests
from botocore.credentials import Credentials

from medical_nudging.search.fhir_auth import (
    HEALTHLAKE_SERVICE_NAME,
    FHIRAuthError,
    SigV4RequestsAuth,
    create_healthlake_auth,
)
from medical_nudging.search.fhir_client import RestFHIRClient

BASE_URL = "https://healthlake.us-east-1.amazonaws.com/datastore/abc123/r4"

FIXED_CREDENTIALS = Credentials(
    access_key="AKIDEXAMPLE",
    secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    token="FwoGZXIvYXdzEXAMPLESESSIONTOKEN",
)


def _auth() -> SigV4RequestsAuth:
    return SigV4RequestsAuth(FIXED_CREDENTIALS, HEALTHLAKE_SERVICE_NAME, "us-east-1")


def _bundle(resources: list[dict], next_url: str | None = None, total: int | None = None) -> dict:
    bundle: dict = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"search": {"mode": "match"}, "resource": r} for r in resources],
        "link": [{"relation": "self", "url": BASE_URL}],
    }
    if total is not None:
        bundle["total"] = total
    if next_url:
        bundle["link"].append({"relation": "next", "url": next_url})
    return bundle


class _Recorder:
    """Capture the PreparedRequests a client sends and serve canned bundles."""

    def __init__(self, bundles: list[dict], status: int = 200):
        self.bundles = bundles
        self.status = status
        self.requests: list[requests.PreparedRequest] = []

    def as_session_send(self):
        """Return a replacement for ``requests.Session.send``."""

        def send(_session, prepared, **kwargs):
            self.requests.append(prepared)
            response = requests.Response()
            response.status_code = self.status
            response.url = prepared.url
            payload = self.bundles[min(len(self.requests) - 1, len(self.bundles) - 1)]
            response._content = json.dumps(payload).encode()
            response.headers["Content-Type"] = "application/fhir+json"
            response.request = prepared
            return response

        return send


def _run(client: RestFHIRClient, bundles: list[dict], **search_kwargs) -> tuple[list, _Recorder]:
    recorder = _Recorder(bundles)
    with patch.object(requests.Session, "send", recorder.as_session_send()):
        resources = client.search("Condition", **search_kwargs)
    return resources, recorder


# ---------------------------------------------------------------------------
# 1. Query-string signing / prepare order
# ---------------------------------------------------------------------------


def test_query_string_is_present_when_auth_runs():
    """prepare_url must run before prepare_auth, or SigV4 signs the wrong URL."""
    seen: dict[str, str] = {}

    class Spy(requests.auth.AuthBase):
        def __call__(self, request):
            seen["url"] = request.url
            return request

    client = RestFHIRClient(BASE_URL, auth=Spy(), search_method="GET")
    _run(client, [_bundle([])], params={"patient": "p1", "clinical-status": "active"})

    assert "?" in seen["url"], "auth ran before the query string was built"
    query = parse_qs(seen["url"].split("?", 1)[1])
    assert query["patient"] == ["p1"]
    assert query["clinical-status"] == ["active"]


def test_signature_covers_the_query_string():
    """Two searches differing only in query params must produce different signatures."""
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="GET")

    _, first = _run(client, [_bundle([])], params={"patient": "p1"})
    _, second = _run(client, [_bundle([])], params={"patient": "p2"})

    sig_one = first.requests[0].headers["Authorization"]
    sig_two = second.requests[0].headers["Authorization"]
    assert sig_one.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "healthlake" in sig_one
    assert sig_one != sig_two, "signature ignored the query string"


def test_session_token_header_is_added():
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="GET")
    _, recorder = _run(client, [_bundle([])], params={"patient": "p1"})
    assert recorder.requests[0].headers["X-Amz-Security-Token"] == FIXED_CREDENTIALS.token


def test_resigning_the_same_request_is_idempotent():
    """Stale signature headers are stripped, so a re-signed request stays valid."""
    auth = _auth()
    prepared = requests.Request("GET", f"{BASE_URL}/Patient", params={"_count": "5"}).prepare()

    auth(prepared)
    auth(prepared)

    authorization = prepared.headers["Authorization"]
    assert authorization.count("AWS4-HMAC-SHA256") == 1
    # The signed-header list must not carry signature headers from the previous
    # pass, or the second signature would cover a canonical request the server
    # cannot reconstruct.
    signed_headers = authorization.split("SignedHeaders=")[1].split(",")[0]
    assert "authorization" not in signed_headers
    assert "x-amz-security-token" in signed_headers


# ---------------------------------------------------------------------------
# 2. POST _search body signing
# ---------------------------------------------------------------------------


def test_post_search_sends_form_encoded_body_and_keeps_params_out_of_the_url():
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="POST")
    _, recorder = _run(client, [_bundle([])], params={"patient": "p1", "clinical-status": "active"})

    sent = recorder.requests[0]
    assert sent.method == "POST"
    assert sent.url == f"{BASE_URL}/Condition/_search"
    assert "patient" not in sent.url
    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"
    body = sent.body.decode() if isinstance(sent.body, bytes) else sent.body
    assert parse_qs(body)["patient"] == ["p1"]
    assert "Authorization" in sent.headers


def test_post_search_preserves_repeated_parameters():
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="POST")
    _, recorder = _run(
        client,
        [_bundle([])],
        params={"patient": "p1", "date": ["ge2142-05-10", "le2142-05-16"]},
    )

    body = recorder.requests[0].body
    body = body.decode() if isinstance(body, bytes) else body
    assert parse_qs(body)["date"] == ["ge2142-05-10", "le2142-05-16"]


def test_post_search_body_is_not_re_encoded_after_signing():
    """The signed payload must be the exact bytes requests puts on the wire."""
    signed_bodies: list[object] = []

    class Spy(requests.auth.AuthBase):
        def __call__(self, request):
            signed_bodies.append(request.body)
            return request

    client = RestFHIRClient(BASE_URL, auth=Spy(), search_method="POST")
    _, recorder = _run(client, [_bundle([])], params={"patient": "p1"})

    assert signed_bodies[0] == recorder.requests[0].body


# ---------------------------------------------------------------------------
# 3. Next-link pagination re-signing
# ---------------------------------------------------------------------------

NEXT_URL = (
    "https://healthlake.us-east-1.amazonaws.com/datastore/abc123/r4/Condition"
    "?patient=p1&_page_token=opaque%2Ftoken%2Bvalue%3D%3D"
)


def test_next_link_is_re_signed_with_its_token_preserved():
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="POST", max_pages=5)
    resources, recorder = _run(
        client,
        [
            _bundle([{"resourceType": "Condition", "id": "c1"}], next_url=NEXT_URL, total=2),
            _bundle([{"resourceType": "Condition", "id": "c2"}]),
        ],
        params={"patient": "p1"},
    )

    assert [r["id"] for r in resources] == ["c1", "c2"]
    assert len(recorder.requests) == 2

    second = recorder.requests[1]
    assert second.method == "GET", "next-link pages are fetched with GET"
    assert second.url == NEXT_URL, "next-link token was altered"
    assert "Authorization" in second.headers
    assert second.headers["Authorization"] != recorder.requests[0].headers["Authorization"]


RAW_NEXT_URL = (
    "https://healthlake.us-east-1.amazonaws.com/datastore/abc123/r4/Condition"
    "?_count=50&patient=p1&page=AAMA-EFRSURBSGd2bzlHTkh2NkZ=="
)


@pytest.mark.parametrize(
    ("next_url", "patient_reference"),
    [
        (RAW_NEXT_URL, "p1"),
        (RAW_NEXT_URL.replace("patient=p1", "patient=Patient%2Fp1"), "Patient/p1"),
    ],
)
def test_next_link_query_is_percent_encoded_before_signing(next_url, patient_reference):
    """HealthLake returns its page token with raw base64 "=" padding.

    SigV4 canonicalization percent-encodes reserved characters inside a query
    value, and the service does the same to what it receives -- so signing and
    sending the link verbatim yields a signature mismatch (HTTP 403) on page 2.
    """
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="POST", max_pages=5)
    resources, recorder = _run(
        client,
        [
            _bundle([{"resourceType": "Condition", "id": "c1"}], next_url=next_url, total=2),
            _bundle([{"resourceType": "Condition", "id": "c2"}]),
        ],
        params={"patient": "p1"},
    )

    assert [r["id"] for r in resources] == ["c1", "c2"]
    sent_url = recorder.requests[1].url
    assert "page=AAMA-EFRSURBSGd2bzlHTkh2NkZ%3D%3D" in sent_url
    assert "=AAMA-EFRSURBSGd2bzlHTkh2NkZ==" not in sent_url
    # The token still decodes back to exactly what the server sent.
    assert parse_qs(sent_url.split("?", 1)[1])["page"] == ["AAMA-EFRSURBSGd2bzlHTkh2NkZ=="]
    assert parse_qs(sent_url.split("?", 1)[1])["patient"] == [patient_reference]


def test_already_encoded_next_link_is_not_double_encoded():
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="POST", max_pages=5)
    _, recorder = _run(
        client,
        [
            _bundle([{"resourceType": "Condition", "id": "c1"}], next_url=NEXT_URL),
            _bundle([]),
        ],
        params={"patient": "p1"},
    )
    assert recorder.requests[1].url == NEXT_URL


def test_connection_headers_are_not_signed():
    """`Connection` is owned by urllib3, so signing it guarantees a mismatch."""
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="GET")
    _, recorder = _run(client, [_bundle([])], params={"patient": "p1"})

    sent = recorder.requests[0]
    signed_headers = sent.headers["Authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert "connection" not in signed_headers.split(";")
    # It is still sent -- SigV4 tolerates unsigned headers.
    assert "Connection" in sent.headers
    assert "host" in signed_headers.split(";")


def test_pagination_stops_at_max_pages_and_warns(caplog):
    client = RestFHIRClient(BASE_URL, auth=_auth(), max_pages=1)
    with caplog.at_level("WARNING"):
        resources, recorder = _run(
            client,
            [_bundle([{"resourceType": "Condition", "id": "c1"}], next_url=NEXT_URL, total=99)],
            params={"patient": "p1"},
        )

    assert len(recorder.requests) == 1
    assert len(resources) == 1
    assert "page cap" in caplog.text


def test_pagination_follows_every_page_by_default():
    client = RestFHIRClient(BASE_URL, auth=_auth())
    resources, recorder = _run(
        client,
        [
            _bundle([{"resourceType": "Condition", "id": "c1"}], next_url=NEXT_URL),
            _bundle([{"resourceType": "Condition", "id": "c2"}], next_url=NEXT_URL + "2"),
            _bundle([{"resourceType": "Condition", "id": "c3"}]),
        ],
        params={"patient": "p1"},
    )
    assert len(recorder.requests) == 3
    assert [r["id"] for r in resources] == ["c1", "c2", "c3"]


# ---------------------------------------------------------------------------
# 4. Bundle handling details
# ---------------------------------------------------------------------------


def test_include_entries_are_dropped():
    bundle = _bundle([{"resourceType": "Condition", "id": "c1"}])
    bundle["entry"].append(
        {"search": {"mode": "include"}, "resource": {"resourceType": "Patient", "id": "p1"}}
    )
    client = RestFHIRClient(BASE_URL, auth=_auth())
    resources, _ = _run(client, [bundle], params={"patient": "p1"})
    assert [r["resourceType"] for r in resources] == ["Condition"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("500", "100"), ("0", "1"), ("20", "20")],
)
def test_count_is_clamped_to_the_server_maximum(requested, expected):
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="GET")
    _, recorder = _run(client, [_bundle([])], params={"_count": requested})
    query = parse_qs(recorder.requests[0].url.split("?", 1)[1])
    assert query["_count"] == [expected]


def test_non_numeric_count_is_dropped():
    client = RestFHIRClient(BASE_URL, auth=_auth(), search_method="GET")
    _, recorder = _run(client, [_bundle([])], params={"_count": "many", "patient": "p1"})
    assert "_count" not in recorder.requests[0].url


# ---------------------------------------------------------------------------
# 5. Auth factory
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, credentials):
        self._credentials = credentials

    def get_credentials(self):
        return self._credentials


def test_create_healthlake_auth_uses_the_healthlake_service_name():
    auth = create_healthlake_auth("us-east-1", boto_session=_FakeSession(FIXED_CREDENTIALS))
    assert auth.service_name == "healthlake"
    assert auth.region == "us-east-1"


def test_create_healthlake_auth_raises_without_credentials():
    with pytest.raises(FHIRAuthError, match="No AWS credentials"):
        create_healthlake_auth("us-east-1", boto_session=_FakeSession(None))
