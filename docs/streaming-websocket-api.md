# Streaming WebSocket API

Real-time streaming of nudge generation via pre-signed WebSocket URLs.

## Architecture

```
Step 1 (REST - protected by API GW + WAF):
  Client --> POST /ws-url (x-api-key) --> API Gateway + WAF --> Lambda Authorizer
          --> ws_url_generator Lambda --> AgentCoreRuntimeClient.generate_presigned_url()
          <-- {ws_url, session_id, expires_in}

Step 2 (WebSocket - direct to AgentCore):
  Client --> wss://bedrock-agentcore.../runtimes/{arn}/ws?X-Amz-Signature=...
          <-> bidirectional WebSocket
  Container /ws endpoint:
    <-- receive JSON: {ccda_xml|fhir_json|patient_data, visit_context, config}
    --> stream events: text*, tool_start, tool_end, complete|error|fatal
    --> close connection
```

The existing HTTP path (`/invocations`) is unchanged and serves as a fallback.

## Getting a Pre-signed URL

```bash
curl -X POST https://<API_ID>.execute-api.<REGION>.amazonaws.com/v1/ws-url \
  -H "x-api-key: <API_KEY>" \
  -H "Content-Type: application/json"
```

**Response:**

```json
{
  "ws_url": "wss://bedrock-agentcore.<REGION>.amazonaws.com/runtimes/<ARN>/ws?...",
  "session_id": "uuid",
  "expires_in": 300,
  "endpoint": "DEFAULT"
}
```

- URL expires in 300 seconds (5 minutes)
- Each URL is single-use per session

### Authentication

The `/ws-url` endpoint requires an `x-api-key` header. The key is stored in AWS Secrets Manager under the name configured during deployment (e.g., `medical-nudging/<name>/api-key`).

Requests without a valid key receive `401 Unauthorized`.

## WebSocket Protocol

### 1. Connect

Open a WebSocket connection to the `ws_url` returned above:

```javascript
const ws = new WebSocket(data.ws_url);
```

### 2. Send Request

Send patient data as JSON. Same schema as `/invocations`. For payloads under 28 KB, send a single message:

```json
{
  "ccda_xml": "<ClinicalDocument>...</ClinicalDocument>",
  "visit_context": {
    "specialty": "general",
    "visit_type": "follow_up",
    "chief_complaint": "Routine diabetes management"
  },
  "config": {}
}
```

**Supported input formats:**

| Field | Type | Description |
|-------|------|-------------|
| `ccda_xml` | string | Raw CCDA XML document |
| `fhir_json` | string | Raw FHIR JSON bundle |
| `patient_data` | object | Pre-parsed patient data (dict with demographics, problems, etc.) |
| `visit_context` | object | Visit metadata (specialty, visit_type, chief_complaint) |
| `config` | object | Generation configuration overrides |

#### Chunked transfer (payloads > 28 KB)

AgentCore enforces a **32 KB WebSocket frame size limit** ([quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)). Raw CCDA XML (~85–300 KB) and FHIR JSON bundles exceed this limit and will cause an immediate disconnect (close code 1009).

The server supports an application-level chunking protocol for large payloads. The client splits the large data field into chunks under 28 KB and sends them as multiple messages:

**Step 1 — Init message:**

```json
{
  "_chunked": true,
  "total_chunks": 4,
  "meta": {
    "_data_field": "ccda_xml",
    "visit_context": {"specialty": "general"},
    "config": {}
  }
}
```

`_data_field` identifies which request field contains the large data (`ccda_xml`, `fhir_json`, or `patient_data`). All other request fields go in `meta`.

**Step 2 — Data chunks** (one message per chunk, in order):

```json
{"_chunk_index": 0, "_chunk_data": "<ClinicalDocument><recordTar..."}
{"_chunk_index": 1, "_chunk_data": "...continuation of XML data..."}
{"_chunk_index": 2, "_chunk_data": "...more data..."}
{"_chunk_index": 3, "_chunk_data": "...ent></ClinicalDocument>"}
```

The server reassembles the chunks into the full data field and processes the request normally. The reference implementation is in `scripts/run_ws_streaming.py` (`send_payload()`).

### 3. Receive Events

The server streams JSON events. Each event has an `event` field and a `data` field.

#### `text`

Incremental text chunk from the agent.

```json
{"event": "text", "data": "Based on the patient's..."}
```

#### `tool_start`

A tool invocation has started.

```json
{
  "event": "tool_start",
  "data": {
    "tool": "search_guidelines",
    "input": {"query": "diabetes management HbA1c targets"}
  }
}
```

#### `tool_end`

A tool invocation has completed.

```json
{
  "event": "tool_end",
  "data": {
    "tool": "search_guidelines",
    "duration_ms": 1234
  }
}
```

#### `complete`

Final result with parsed nudges. Connection closes after this event.

```json
{
  "event": "complete",
  "data": {
    "status": "success",
    "patient_summary": "...",
    "nudges": [
      {
        "title": "Intensify diabetes medications",
        "urgency": "high",
        "category": "medication",
        "rationale": "...",
        "citations": [...]
      }
    ],
    "usage": {"input_tokens": 15000, "output_tokens": 3000},
    "processing_time_ms": 45000,
    "request_id": "uuid"
  }
}
```

#### `error`

Recoverable error. Connection stays open.

```json
{
  "event": "error",
  "data": {"code": "SEARCH_FAILED", "message": "...", "recoverable": true}
}
```

#### `fatal`

Unrecoverable error. Connection closes after this event.

```json
{
  "event": "fatal",
  "data": {"code": "INVALID_JSON", "message": "Expecting value: line 1 column 1"}
}
```

**Fatal codes:**

| Code | Cause |
|------|-------|
| `INVALID_JSON` | Request body (or chunk) is not valid JSON |
| `INVALID_REQUEST` | Missing required fields or invalid schema |
| `INVALID_CHUNKED_REQUEST` | Chunked init message missing `_data_field` or `total_chunks` |
| `INVALID_CHUNK` | Chunk index out of range or missing |
| `PARSE_ERROR` | CCDA/FHIR parsing failed |
| `INVALID_FORMAT` | Patient data format not recognized |
| `INTERNAL_ERROR` | Server-side error during processing |

### 4. Connection Lifecycle

```
Client                          Server
  |--- WebSocket CONNECT ---------->|
  |<-- 101 Switching Protocols -----|
  |--- JSON request --------------->|
  |<-- {"event":"text",...} --------|  (repeated)
  |<-- {"event":"tool_start",...} --|
  |<-- {"event":"text",...} --------|  (repeated)
  |<-- {"event":"tool_end",...} ----|
  |<-- {"event":"complete",...} ----|
  |<-- Connection Close ------------|
```

## Deployment

### Via deploy script (recommended)

```bash
./scripts/deploy_agentcore.sh \
  --profile <aws-profile> \
  --name <runtime-name>
```

This creates:
- Docker image with `/ws` endpoint (ARM64)
- AgentCore runtime
- API key in Secrets Manager (`medical-nudging/<name>/api-key`)
- API Gateway REST API with Lambda Authorizer
- ws_url_generator Lambda + bedrock-agentcore SDK layer
- WAF Web ACL (managed rules, rate limiting, body size constraint)

The WebSocket API is the only client path the deploy script creates; there is no public HTTP proxy in front of the runtime.

### Via Terraform

`websocket_api_enabled` defaults to `true`, so a plain apply creates the same resources:

```bash
cd terraform
terraform apply -var="aws_profile=<profile>"
```

### WAF Protection

The API Gateway is protected by a WAF Web ACL with:

| Rule | Priority | Action |
|------|----------|--------|
| AWSManagedRulesCommonRuleSet | 1 | Block (body size override: count) |
| AWSManagedRulesKnownBadInputsRuleSet | 2 | Block |
| Rate limit (100 req/5min/IP) | 3 | Block |
| Body size > 8 KB | 4 | Block |

WAF only covers Step 1 (REST API). Step 2 (direct WebSocket to AgentCore) is protected by the pre-signed URL's 5-minute expiry and SigV4 signature.

## Testing

### Manual curl test

```bash
# Get pre-signed URL
API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id medical-nudging/<name>/api-key \
  --query SecretString --output text | python3 -c "import sys,json; print(json.load(sys.stdin)['api_key'])")

curl -s -X POST "https://<API_ID>.execute-api.<REGION>.amazonaws.com/v1/ws-url" \
  -H "x-api-key: $API_KEY" | python3 -m json.tool
```

### E2E streaming test script

```bash
# Via pre-signed URL (deployed)
uv run scripts/run_ws_streaming.py \
  --api-url "https://<API_ID>.execute-api.<REGION>.amazonaws.com/v1/ws-url" \
  --api-key "$API_KEY" \
  --patient-file tests/fixtures/sample_ccda.xml

# Direct local WebSocket
uv run scripts/run_ws_streaming.py \
  --url ws://localhost:8080/ws \
  --patient-file data/sample-ccda/file.xml
```

### Local development

```bash
# Start the agent server locally
uv run uvicorn agent:app --port 8080

# Connect directly (no pre-signed URL needed locally)
uv run scripts/run_ws_streaming.py \
  --url ws://localhost:8080/ws \
  --patient-file tests/fixtures/sample_ccda.xml
```

## Security

- **Step 1 (REST)**: API key authentication via Lambda Authorizer + WAF protection
- **Step 2 (WebSocket)**: SigV4 pre-signed URL with 5-minute expiry
- **No WAF on Step 2**: Trade-off accepted; mitigated by short-lived URLs and AgentCore's own protections
- **No unauthenticated endpoints**: the runtime is reachable only through `POST /ws-url` (API key + WAF) and the presigned WebSocket URL it returns; nothing is exposed with auth type NONE

## Known Limitations

- **32 KB WebSocket frame size limit**: AgentCore enforces a non-adjustable 32 KB frame size limit ([quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)). Raw CCDA XML (~85–300 KB) and FHIR JSON bundles exceed this as a single message. **Workaround**: use the [chunked transfer protocol](#chunked-transfer-payloads--28-kb) to split large payloads into <28 KB chunks. The server reassembles them transparently. Pre-parsed patient data (typically 1–5 KB) and the bundled `tests/fixtures/sample_ccda.xml` (9 KB) do not require chunking.
- Hospital firewalls may block WebSocket connections; HTTP sync path serves as fallback
- Pre-signed URLs are single-use per session
- Other AgentCore WebSocket limits: 250 frames/sec rate, 60 min max streaming duration, 15 min idle timeout
