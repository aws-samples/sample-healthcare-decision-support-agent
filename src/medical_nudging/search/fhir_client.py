"""REST client for querying a FHIR R4 server.

Thin wrapper around ``requests`` that handles FHIR bundle responses and
searchset pagination. The client is transport-generic: authentication is
injected as a ``requests.auth.AuthBase``, so the same code talks to AWS
HealthLake (SigV4), a bearer-token FHIR endpoint, or an API-key endpoint.

The sample ships and exposes only the HealthLake configuration — see
``docs/fhir-api-integration.md`` for the portability trade-off.

Usage:
    from medical_nudging.search.fhir_client import RestFHIRClient, create_healthlake_client

    # AWS HealthLake — base URL is the datastore's DatastoreEndpoint, never assembled
    client = create_healthlake_client(datastore_endpoint, region="us-east-1")

    # Any other FHIR R4 server (bearer token / API key / injected auth)
    client = RestFHIRClient("https://fhir.example.com/r4", bearer_token="...")

    patients = client.search("Patient", {"_count": "5"})
    conditions = client.search("Condition", {"patient": "abc123", "clinical-status": "active"})
    patient = client.read("Patient", "abc123")
"""

import logging
import threading
from typing import Any, Literal
from urllib.parse import quote, unquote_to_bytes, urlencode, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

SearchMethod = Literal["GET", "POST"]
FHIRSearchValue = str | list[str]
FHIRSearchParams = dict[str, FHIRSearchValue]

# HealthLake caps page size at 100 and rejects _count=0. Clamping here keeps an
# agent-supplied _count from turning into a 4xx.
MIN_COUNT = 1
MAX_COUNT = 100

# Default cap on searchset pages followed per search(). Truncation is a logged
# choice, not a silent one.
DEFAULT_MAX_PAGES = 10


class FHIRClientError(Exception):
    """Raised when a FHIR API request fails."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RestFHIRClient:
    """REST client for FHIR R4 search and read operations.

    Args:
        base_url: FHIR server base URL. For HealthLake this is the datastore's
            ``DatastoreEndpoint`` verbatim — do not hand-assemble it.
        api_key: Optional API key sent as an ``X-API-Key`` header.
        bearer_token: Optional bearer token sent as an ``Authorization`` header.
        auth: Optional ``requests`` auth object, e.g. the SigV4 signer from
            :func:`medical_nudging.search.fhir_auth.create_healthlake_auth`.
        timeout: Request timeout in seconds (default 30).
        search_method: ``"GET"`` for ``GET /<Type>?<params>``, or ``"POST"`` for
            ``POST /<Type>/_search`` with form-encoded params. POST keeps patient
            identifiers out of URLs (and therefore out of access logs), which is
            the recommended shape for PHI.
        max_pages: Maximum searchset pages followed per search (default 10).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        bearer_token: str | None = None,
        auth: requests.auth.AuthBase | None = None,
        timeout: int = 30,
        search_method: SearchMethod = "GET",
        max_pages: int = DEFAULT_MAX_PAGES,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.search_method: SearchMethod = search_method
        self.max_pages = max(1, max_pages)
        # Per-thread flag: whether this thread's most recent search() stopped at its
        # page cap with more pages available.  Callers that record query coverage read
        # it after each search — a page-capped window cannot establish absence
        # (coverage-v2).  Thread-local because one client is shared across tool calls
        # that a concurrent tool executor may run on different threads; a shared bool
        # would let call B's reset erase call A's cap before A reads it.
        self._page_cap_state = threading.local()
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/fhir+json"
        if auth is not None:
            self.session.auth = auth
        if api_key:
            self.session.headers["X-API-Key"] = api_key
        if bearer_token:
            self.session.headers["Authorization"] = f"Bearer {bearer_token}"

    @property
    def last_search_hit_page_cap(self) -> bool:
        """Whether this thread's most recent ``search()`` stopped at its page cap."""
        return getattr(self._page_cap_state, "hit", False)

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        """Read a single FHIR resource by type and ID.

        Args:
            resource_type: FHIR resource type (e.g., "Patient")
            resource_id: Resource ID

        Returns:
            The FHIR resource as a dict.

        Raises:
            FHIRClientError: If the request fails.
        """
        url = f"{self.base_url}/{resource_type}/{resource_id}"
        logger.debug(f"FHIR read: GET {url}")
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            raise FHIRClientError(
                f"FHIR read {resource_type}/{resource_id} failed: HTTP {status}",
                status_code=status,
            ) from e
        except requests.ConnectionError as e:
            raise FHIRClientError(f"Cannot connect to FHIR server at {self.base_url}") from e

    def search(
        self,
        resource_type: str,
        params: FHIRSearchParams | None = None,
        max_pages: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search for FHIR resources. Returns the matched resources (not the Bundle).

        Follows ``Bundle.link[relation="next"]`` up to ``max_pages`` pages. The
        next-link query string is percent-encoded before the request is signed —
        see :func:`_normalize_next_url` for why fetching it verbatim fails. Entries whose
        ``search.mode`` is ``"include"`` are dropped so ``_include`` /
        ``_revinclude`` results never masquerade as matches.

        Args:
            resource_type: FHIR resource type (e.g., "Condition", "Observation")
            params: FHIR search parameters (e.g., {"patient": "abc", "clinical-status": "active"})
            max_pages: Page cap for this call; defaults to the client's ``max_pages``.

        Returns:
            List of matched FHIR resource dicts from the Bundle entries.

        Raises:
            FHIRClientError: If the request fails.
        """
        page_limit = self.max_pages if max_pages is None else max(1, max_pages)
        search_params = _clamp_count(params or {})
        self._page_cap_state.hit = False

        resources: list[dict[str, Any]] = []
        next_url: str | None = None
        bundle_total: int | None = None

        for page in range(page_limit):
            if next_url is None:
                bundle = self._first_page(resource_type, search_params)
            else:
                logger.debug(f"FHIR search: GET next page {page + 1} for {resource_type}")
                bundle = self._request_bundle("GET", next_url, resource_type=resource_type)

            resources.extend(_match_entries(bundle))
            if bundle_total is None and isinstance(bundle.get("total"), int):
                bundle_total = bundle["total"]

            next_url = _next_link(bundle)
            if next_url is None:
                return resources

        self._page_cap_state.hit = True
        logger.warning(
            f"FHIR search {resource_type}: stopped at the {page_limit}-page cap with more "
            f"pages available ({len(resources)} of {bundle_total if bundle_total is not None else 'unknown'} "
            "resources returned). Narrow the query or raise max_pages."
        )
        return resources

    def metadata(self) -> dict[str, Any]:
        """Get the server's CapabilityStatement (health check / feature discovery)."""
        url = f"{self.base_url}/metadata"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.ConnectionError as e:
            raise FHIRClientError(f"Cannot connect to FHIR server at {self.base_url}") from e

    # -- internals ---------------------------------------------------------

    def _first_page(self, resource_type: str, params: FHIRSearchParams) -> dict[str, Any]:
        """Issue the initial search, either as GET with a query string or POST _search."""
        if self.search_method == "POST":
            url = f"{self.base_url}/{resource_type}/_search"
            logger.debug(f"FHIR search: POST {url} ({len(params)} params in body)")
            # Pass the pre-encoded body so requests cannot re-encode it after
            # signing; HealthLake requires x-www-form-urlencoded and forbids
            # mixing URL params with body params.
            return self._request_bundle(
                "POST",
                url,
                resource_type=resource_type,
                data=urlencode(params, doseq=True),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        url = f"{self.base_url}/{resource_type}"
        logger.debug(f"FHIR search: GET {url} params={params}")
        return self._request_bundle("GET", url, resource_type=resource_type, params=params)

    def _request_bundle(
        self,
        method: SearchMethod,
        url: str,
        resource_type: str,
        params: FHIRSearchParams | None = None,
        data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            raise FHIRClientError(
                f"FHIR search {resource_type} failed: HTTP {status}",
                status_code=status,
            ) from e
        except requests.ConnectionError as e:
            raise FHIRClientError(f"Cannot connect to FHIR server at {self.base_url}") from e


def create_healthlake_client(
    datastore_endpoint: str,
    region: str,
    timeout: int = 30,
    search_method: SearchMethod = "POST",
    max_pages: int = DEFAULT_MAX_PAGES,
) -> RestFHIRClient:
    """Build a client for an AWS HealthLake FHIR datastore.

    Args:
        datastore_endpoint: The datastore's ``DatastoreEndpoint`` (terraform
            output ``healthlake_datastore_endpoint``), used verbatim.
        region: AWS region hosting the datastore.
        timeout: Request timeout in seconds.
        search_method: ``"POST"`` (default) keeps patient ids out of request URLs.
        max_pages: Maximum searchset pages followed per search.

    Returns:
        A configured :class:`RestFHIRClient` with SigV4 signing installed.
    """
    from medical_nudging.search.fhir_auth import create_healthlake_auth

    return RestFHIRClient(
        base_url=datastore_endpoint,
        auth=create_healthlake_auth(region),
        timeout=timeout,
        search_method=search_method,
        max_pages=max_pages,
    )


def _clamp_count(params: FHIRSearchParams) -> FHIRSearchParams:
    """Clamp ``_count`` into HealthLake's accepted 1..100 range."""
    raw = params.get("_count")
    if raw is None:
        return dict(params)

    clamped = dict(params)
    if isinstance(raw, list):
        logger.warning(f"Ignoring repeated _count={raw!r}; dropping the parameter")
        clamped.pop("_count")
        return clamped

    try:
        count = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring non-numeric _count={raw!r}; dropping the parameter")
        clamped.pop("_count")
        return clamped

    bounded = min(max(count, MIN_COUNT), MAX_COUNT)
    if bounded != count:
        logger.warning(f"Clamped _count={count} to {bounded} (server maximum is {MAX_COUNT})")
    clamped["_count"] = str(bounded)
    return clamped


def _match_entries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract resources from Bundle entries, dropping _include results."""
    resources: list[dict[str, Any]] = []
    for entry in bundle.get("entry", []):
        if not isinstance(entry, dict) or "resource" not in entry:
            continue
        search_block = entry.get("search")
        mode = search_block.get("mode") if isinstance(search_block, dict) else None
        if mode == "include":
            continue
        resources.append(entry["resource"])
    return resources


def _next_link(bundle: dict[str, Any]) -> str | None:
    """Return the Bundle's next-page URL, or None when the searchset is exhausted."""
    for link in bundle.get("link", []):
        if not isinstance(link, dict):
            continue
        if link.get("relation") == "next" and isinstance(link.get("url"), str):
            return _normalize_next_url(link["url"])
    return None


def _normalize_next_url(url: str) -> str:
    """Percent-encode a next-link's query string so SigV4 signing succeeds.

    HealthLake returns its ``page`` continuation token as raw base64, so the URL
    carries unencoded ``=`` padding characters. SigV4 canonicalization requires
    reserved characters inside a query *value* to be percent-encoded, and the
    service canonicalizes what it receives the same way — so signing and sending
    the link verbatim produces "The request signature we calculated does not
    match the signature you provided" (HTTP 403) on every page after the first.

    Re-encoding is safe because the token is opaque only to *us*: the server
    decodes ``%3D`` back to ``=`` before interpreting it. Normalize each component
    independently: an encoded patient reference can coexist with raw token padding.
    Decode existing escapes once before encoding, so they cannot be double-encoded.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url

    # Split by hand rather than with parse_qsl: parse_qsl decodes "+" to a space,
    # which would corrupt a base64 token that legitimately contains "+".
    encoded_pairs = []
    for pair in parts.query.split("&"):
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        if sep:
            encoded_pairs.append(
                f"{quote(unquote_to_bytes(name), safe='')}="
                f"{quote(unquote_to_bytes(value), safe='')}"
            )
        else:
            encoded_pairs.append(quote(unquote_to_bytes(name), safe=""))
    if not encoded_pairs:
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(encoded_pairs), parts.fragment)
    )
