# Observability Guide

This guide covers how to use CloudWatch metrics and Athena analytics for monitoring the Medical Nudging system.

## Quick Reference

| Resource | Console URL | CLI Command |
|----------|-------------|-------------|
| CloudWatch Dashboard | [medical-nudging-dashboard](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/medical-nudging-dashboard) | `aws cloudwatch get-dashboard --dashboard-name medical-nudging-dashboard` |
| CloudWatch Metrics | [MedicalNudging namespace](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#metricsV2?graph=~()&namespace=MedicalNudging) | `aws cloudwatch list-metrics --namespace MedicalNudging` |
| Runtime Logs | [AgentCore Logs](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws$252Fbedrock-agentcore$252Fruntimes$252FYOUR_RUNTIME_ID-DEFAULT) | `aws logs filter-log-events --log-group-name "/aws/bedrock-agentcore/runtimes/..."` |
| Athena Workgroup | [medical-nudging-observability](https://us-east-1.console.aws.amazon.com/athena/home?region=us-east-1#/workgroup/medical-nudging-observability) | `aws athena list-named-queries --work-group medical-nudging-observability` |
| S3 Events Bucket | [medical-nudging-observability-123456789012](https://s3.console.aws.amazon.com/s3/buckets/medical-nudging-observability-123456789012?region=us-east-1&prefix=events/) | `aws s3 ls s3://medical-nudging-observability-123456789012/events/` |
| OTEL Spans (CloudWatch) | [aws/spans](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:logs-insights$3FqueryDetail$3D~(source~(~'aws*2fspans))) | `aws logs start-query --log-group-name aws/spans ...` |
| OTEL Spans (S3) | [medical-nudging-otel-spans-123456789012](https://s3.console.aws.amazon.com/s3/buckets/medical-nudging-otel-spans-123456789012?region=us-east-1&prefix=data/) | `aws s3 ls s3://medical-nudging-otel-spans-123456789012/data/` |
| Runtime Logs (S3) | [medical-nudging-runtime-logs-123456789012](https://s3.console.aws.amazon.com/s3/buckets/medical-nudging-runtime-logs-123456789012?region=us-east-1&prefix=data/) | `aws s3 ls s3://medical-nudging-runtime-logs-123456789012/data/` |
| S3 Traces | [traces/](https://s3.console.aws.amazon.com/s3/buckets/medical-nudging-observability-123456789012?region=us-east-1&prefix=traces/) | `aws s3 ls s3://medical-nudging-observability-123456789012/traces/` |
| Athena Traces Table | `medical_nudging_observability.traces` | See [Athena Traces Table](#athena-traces-table) section |
| Online evaluation results | `terraform output online_evaluation_results_log_group` | `aws logs tail "$(terraform -chdir=terraform output -raw online_evaluation_results_log_group)"` |
| Evaluation score metrics | `Bedrock-AgentCore/Evaluations` namespace | `aws cloudwatch list-metrics --namespace Bedrock-AgentCore/Evaluations` |

Online evaluation is opt-in (`online_evaluation_enabled`, default `false`). When on,
AgentCore samples completed sessions, scores them asynchronously with the configured
evaluators, writes one JSON event per evaluation to the results log group, and
publishes each score as a metric. Read the scores as trend lines next to latency,
errors, cost, and tool health. They are not evidence of clinical safety or
correctness. Setup, sampling guidance, and the candidate gate are in the
[deployment guide](deployment-guide.md#candidate-acceptance-gate-and-online-evaluation).

---

## CloudWatch Metrics

### Available Metrics

| Metric | Description | Dimensions |
|--------|-------------|------------|
| `RequestCount` | Total inference requests | Environment, Specialty, VisitType |
| `SuccessCount` | Successful completions | Environment, Specialty, VisitType |
| `ErrorCount` | Failed requests | Environment, Specialty, VisitType |
| `RequestLatency` | End-to-end latency (ms) | Environment, Specialty, VisitType |
| `ToolCallCount` | Tool invocations | Environment, ToolName |
| `NudgeCount` | Nudges generated | Environment, Category |

### Console Usage

1. Open [CloudWatch Metrics Console](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#metricsV2?graph=~()&namespace=MedicalNudging)
2. Select **MedicalNudging** namespace
3. Choose dimensions (e.g., Environment → dev)
4. Select metrics to graph

### CLI Usage

```bash
# List all metrics in namespace
aws cloudwatch list-metrics \
  --namespace "MedicalNudging" \
  --region us-east-1

# Get RequestCount for last hour
aws cloudwatch get-metric-statistics \
  --namespace "MedicalNudging" \
  --metric-name "RequestCount" \
  --dimensions Name=Environment,Value=dev \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum \
  --region us-east-1

# Get RequestLatency percentiles
aws cloudwatch get-metric-statistics \
  --namespace "MedicalNudging" \
  --metric-name "RequestLatency" \
  --dimensions Name=Environment,Value=dev \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --extended-statistics p50 p95 p99 \
  --region us-east-1

# Get metrics by specialty
aws cloudwatch get-metric-statistics \
  --namespace "MedicalNudging" \
  --metric-name "SuccessCount" \
  --dimensions Name=Environment,Value=dev Name=Specialty,Value=cardiology \
  --start-time $(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Sum \
  --region us-east-1
```

### CloudWatch Dashboard

Open the pre-built dashboard: [medical-nudging-dashboard](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/medical-nudging-dashboard)

The dashboard includes:
- Request volume and success rate
- Latency percentiles (p50, p95, p99)
- Error rate trends
- Tool call breakdown
- Nudge category distribution

---

## Athena Analytics

### Database Schema

- **Database**: `medical_nudging_observability`
- **Table**: `events`
- **Workgroup**: `medical-nudging-observability`

### Event Types

| Event Type | Description |
|------------|-------------|
| `request_received` | Inference request started |
| `processing_started` | Agent processing began |
| `tool_started` | Tool invocation started |
| `strands_metrics` | Strands SDK metrics (token_usage, performance, tool_summary, cycles, trace_s3_key) |
| `response_sent` | Response returned (includes token_usage, nudge_breakdown) |
| `error` | Error occurred (orchestrator level) |

### Console Usage

1. Open [Athena Query Editor](https://us-east-1.console.aws.amazon.com/athena/home?region=us-east-1#/query-editor)
2. Select workgroup: **medical-nudging-observability**
3. Select database: **medical_nudging_observability**
4. Run queries against the `events` table

### CLI Usage

```bash
# Start a query
QUERY_ID=$(aws athena start-query-execution \
  --query-string "SELECT event_type, count(*) as cnt FROM events WHERE year='2026' AND month='01' GROUP BY event_type" \
  --work-group "medical-nudging-observability" \
  --region us-east-1 \
  --output text --query 'QueryExecutionId')

echo "Query ID: $QUERY_ID"

# Wait for completion (poll status)
aws athena get-query-execution \
  --query-execution-id $QUERY_ID \
  --region us-east-1 \
  --query 'QueryExecution.Status.State'

# Get results
aws athena get-query-results \
  --query-execution-id $QUERY_ID \
  --region us-east-1
```

### Quick Start Queries

```sql
-- Query recent events
SELECT 
    event_type,
    timestamp,
    correlation.request_id,
    correlation.session_id,
    payload
FROM medical_nudging_observability.events
WHERE year = '2026' 
  AND month = '02' 
  AND day = '04'
ORDER BY timestamp DESC
LIMIT 20;

-- Query by event type
SELECT *
FROM medical_nudging_observability.events
WHERE year = '2026' AND month = '02' AND day = '04'
  AND event_type = 'response_sent'
LIMIT 10;

-- Count events by type today
SELECT event_type, COUNT(*) as count
FROM medical_nudging_observability.events
WHERE year = '2026' AND month = '02' AND day = '04'
GROUP BY event_type;
```

### Sample Queries

#### Event counts by type (today)
```sql
SELECT event_type, count(*) as count
FROM events
WHERE year = '2026' AND month = '01' AND day = '28'
GROUP BY event_type
ORDER BY count DESC;
```

#### Average latency by specialty
```sql
-- Join request_received with response_sent to get specialty
SELECT 
  json_extract_scalar(req.payload, '$.visit_context.specialty') as specialty,
  avg(cast(json_extract_scalar(resp.payload, '$.processing_time_ms') as double)) as avg_latency_ms,
  count(*) as request_count
FROM events req
JOIN events resp 
  ON req.correlation.request_id = resp.correlation.request_id
WHERE req.event_type = 'request_received'
  AND resp.event_type = 'response_sent'
  AND req.year = '2026' AND req.month = '02'
  AND resp.year = '2026' AND resp.month = '02'
GROUP BY json_extract_scalar(req.payload, '$.visit_context.specialty');
```

#### Tool usage breakdown
```sql
SELECT 
  json_extract_scalar(payload, '$.tool_name') as tool,
  count(*) as calls
FROM events
WHERE event_type = 'tool_started'
  AND year = '2026' AND month = '02'
GROUP BY json_extract_scalar(payload, '$.tool_name')
ORDER BY calls DESC;
```

#### Token usage analysis
```sql
SELECT 
  date(from_iso8601_timestamp(timestamp)) as date,
  sum(cast(json_extract_scalar(payload, '$.token_usage.input_tokens') as bigint)) as total_input_tokens,
  sum(cast(json_extract_scalar(payload, '$.token_usage.output_tokens') as bigint)) as total_output_tokens
FROM events
WHERE event_type = 'response_sent'
  AND year = '2026' AND month = '02'
  AND json_extract_scalar(payload, '$.token_usage') IS NOT NULL
GROUP BY date(from_iso8601_timestamp(timestamp))
ORDER BY 1;
```

#### Nudge distribution by category
```sql
SELECT 
  json_format(json_extract(payload, '$.nudge_breakdown.by_category')) as category_breakdown,
  count(*) as requests
FROM events
WHERE event_type = 'response_sent'
  AND year = '2026' AND month = '02'
  AND json_extract(payload, '$.nudge_breakdown') IS NOT NULL
GROUP BY json_format(json_extract(payload, '$.nudge_breakdown.by_category'))
LIMIT 20;
```

#### Error analysis
```sql
SELECT 
  event_type,
  json_extract_scalar(payload, '$.error_type') as error_type,
  json_extract_scalar(payload, '$.error_message') as message,
  count(*) as occurrences
FROM events
WHERE event_type IN ('error', 'api_error')
  AND year = '2026' AND month = '02'
GROUP BY 
  event_type,
  json_extract_scalar(payload, '$.error_type'),
  json_extract_scalar(payload, '$.error_message')
ORDER BY occurrences DESC
LIMIT 10;
```

#### Request trace (by request_id)
```sql
SELECT 
  timestamp,
  event_type,
  source.component as source_component,
  source.mode as source_mode,
  payload
FROM events
WHERE correlation.request_id = 'YOUR_REQUEST_ID'
ORDER BY timestamp;
```

#### Hourly request volume
```sql
SELECT 
  year, month, day,
  hour(from_iso8601_timestamp(timestamp)) as hour,
  count(*) as requests
FROM events
WHERE event_type = 'request_received'
  AND year = '2026' AND month = '02'
GROUP BY year, month, day, hour(from_iso8601_timestamp(timestamp))
ORDER BY year, month, day, hour;
```

---

## Combining Metrics and Athena

### Use Case: Investigate High Latency

1. **Identify the spike** in CloudWatch:
   ```bash
   aws cloudwatch get-metric-statistics \
     --namespace "MedicalNudging" \
     --metric-name "RequestLatency" \
     --dimensions Name=Environment,Value=dev \
     --start-time 2026-01-28T20:00:00Z \
     --end-time 2026-01-28T22:00:00Z \
     --period 300 \
     --statistics Average Maximum \
     --region us-east-1
   ```

2. **Drill down** in Athena:
   ```sql
   SELECT 
     correlation.request_id,
     json_extract_scalar(payload, '$.processing_time_ms') as latency_ms,
     json_extract_scalar(payload, '$.specialty') as specialty
   FROM events
   WHERE event_type = 'response_sent'
     AND year = '2026' AND month = '01' AND day = '28'
     AND hour BETWEEN '20' AND '22'
   ORDER BY cast(json_extract_scalar(payload, '$.processing_time_ms') as double) DESC
   LIMIT 10;
   ```

3. **Trace the slow request**:
   ```sql
   SELECT timestamp, event_type, payload
   FROM events
   WHERE correlation.request_id = 'slow-request-id-here'
   ORDER BY timestamp;
   ```

---

## Alarms

Pre-configured CloudWatch alarms:

| Alarm | Condition | Action |
|-------|-----------|--------|
| High Error Rate | ErrorCount > 5 in 5 min | SNS notification |
| High Latency | p95 > 120000ms | SNS notification |
| Low Success Rate | SuccessCount < 1 in 15 min | SNS notification |

View alarms: [CloudWatch Alarms Console](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#alarmsV2:)

```bash
# List alarms
aws cloudwatch describe-alarms \
  --alarm-name-prefix "medical-nudging" \
  --region us-east-1

# Get alarm history
aws cloudwatch describe-alarm-history \
  --alarm-name "medical-nudging-high-error-rate" \
  --region us-east-1
```

---

## S3 Event Data

Raw events are stored in S3 with the following structure:

```
s3://medical-nudging-observability-123456789012/
└── events/
    └── year=2026/
        └── month=01/
            └── day=28/
                └── hour=21/
                    └── <event-id>.json
```

### Browse events

```bash
# List recent events
aws s3 ls s3://medical-nudging-observability-123456789012/events/year=2026/month=02/day=03/ --recursive

# Download and view an event
aws s3 cp s3://medical-nudging-observability-123456789012/events/year=2026/month=02/day=03/hour=21/abc123.json - | jq .
```

### S3 Traces

Agent execution traces are stored in the `traces/` prefix as gzip-compressed JSON files. The `strands_metrics` event includes a `trace_s3_key` field pointing to the trace file.

```
s3://medical-nudging-observability-123456789012/
└── traces/
    └── year=2026/
        └── month=02/
            └── day=03/
                └── <request-id>.json.gz
```

```bash
# List recent traces
aws s3 ls s3://medical-nudging-observability-123456789012/traces/ --recursive | tail -10

# Download and view a trace
aws s3 cp s3://medical-nudging-observability-123456789012/traces/year=2026/month=02/day=03/<request-id>.json.gz - | gunzip | jq .
```

Trace files contain:
- `request_id`: Correlation ID
- `timestamp`: When the trace was captured
- `traces`: Raw trace events (if available)
- `tool_metrics`: Per-tool execution metrics
- `accumulated_usage`: Token usage (input, output, cache)
- `accumulated_metrics`: Performance metrics (latency, TTFB)
- `cycle_count`: Number of agent cycles
- `cycle_durations`: Duration of each cycle

### Athena Traces Table

Query traces via Athena using the `medical_nudging_observability.traces` table.

**Create table (if not exists):**
```sql
CREATE EXTERNAL TABLE IF NOT EXISTS medical_nudging_observability.traces (
  request_id STRING,
  timestamp STRING,
  cycle_count INT,
  accumulated_usage STRUCT<
    inputTokens: BIGINT,
    outputTokens: BIGINT,
    totalTokens: BIGINT,
    cacheReadInputTokens: BIGINT,
    cacheWriteInputTokens: BIGINT
  >,
  accumulated_metrics STRUCT<latencyMs: BIGINT>,
  tool_metrics MAP<STRING, STRUCT<call_count: INT, total_duration_ms: DOUBLE, avg_duration_ms: DOUBLE>>,
  cycle_durations ARRAY<DOUBLE>
)
PARTITIONED BY (year STRING, month STRING, day STRING)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES ('ignore.malformed.json' = 'true')
LOCATION 's3://medical-nudging-observability-123456789012/traces/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.year.type' = 'integer',
  'projection.year.range' = '2024,2030',
  'projection.month.type' = 'integer',
  'projection.month.range' = '1,12',
  'projection.month.digits' = '2',
  'projection.day.type' = 'integer',
  'projection.day.range' = '1,31',
  'projection.day.digits' = '2',
  'storage.location.template' = 's3://medical-nudging-observability-123456789012/traces/year=${year}/month=${month}/day=${day}',
  'compressionType' = 'gzip'
);
```

**Query traces:**
```sql
-- List traces for a day
SELECT 
  request_id,
  timestamp,
  cycle_count,
  accumulated_usage.inputTokens as input_tokens,
  accumulated_usage.outputTokens as output_tokens,
  accumulated_metrics.latencyMs as latency_ms
FROM medical_nudging_observability.traces
WHERE year = '2026' AND month = '02' AND day = '03'
ORDER BY timestamp DESC;

-- Token usage summary by day
SELECT 
  year, month, day,
  COUNT(*) as requests,
  SUM(accumulated_usage.inputTokens) as total_input,
  SUM(accumulated_usage.outputTokens) as total_output,
  AVG(accumulated_metrics.latencyMs) as avg_latency_ms
FROM medical_nudging_observability.traces
WHERE year = '2026' AND month = '02'
GROUP BY year, month, day;

-- Find slow requests (>60s)
SELECT request_id, timestamp, accumulated_metrics.latencyMs / 1000.0 as seconds
FROM medical_nudging_observability.traces
WHERE year = '2026' AND accumulated_metrics.latencyMs > 60000
ORDER BY accumulated_metrics.latencyMs DESC;

-- Tool usage breakdown
SELECT 
  t.request_id,
  tool.key as tool_name,
  tool.value.call_count,
  tool.value.avg_duration_ms
FROM medical_nudging_observability.traces t
CROSS JOIN UNNEST(tool_metrics) AS tool(key, value)
WHERE year = '2026' AND month = '02' AND day = '03';
```

---

## OTEL Trace Retrieval

AgentCore emits OpenTelemetry traces to the `aws/spans` CloudWatch log group. These can be queried via CloudWatch Logs Insights or exported to S3 for Athena queries.

### CloudWatch Logs Insights (Real-time)

CloudWatch Logs Insights provides real-time querying of OTEL spans directly in CloudWatch.

**Console:** [CloudWatch Logs Insights](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:logs-insights)

Select log group: `aws/spans`

#### Sample Queries

**List recent spans:**
```
fields @timestamp, @message
| sort @timestamp desc
| limit 20
```

**Find spans by trace ID:**
```
fields @timestamp, @message
| filter @message like /YOUR_TRACE_ID/
| sort @timestamp asc
```

**Find spans by session ID:**
```
fields @timestamp, @message
| filter @message like /YOUR_SESSION_ID/
| sort @timestamp asc
```

**Parse span details:**
```
fields @timestamp, @message
| parse @message '"name":"*"' as spanName
| parse @message '"traceId":"*"' as traceId
| filter spanName like /tool/
| sort @timestamp desc
| limit 50
```

**Tool execution times:**
```
fields @timestamp, @message
| parse @message '"name":"*"' as spanName
| parse @message '"duration_ms":*,' as durationMs
| filter spanName like /tool/
| stats avg(durationMs), max(durationMs), count() by spanName
```

#### CLI Usage

```bash
# Start query
QUERY_ID=$(aws logs start-query \
  --log-group-name "aws/spans" \
  --start-time $(date -d '1 hour ago' +%s) \
  --end-time $(date +%s) \
  --query-string "fields @timestamp, @message | sort @timestamp desc | limit 20" \
  --region us-east-1 \
  --output text --query 'queryId')

echo "Query ID: $QUERY_ID"

# Wait for completion
sleep 5

# Get results
aws logs get-query-results --query-id $QUERY_ID --region us-east-1
```

#### Permissions Required

- `logs:StartQuery`
- `logs:GetQueryResults`
- `logs:StopQuery`

**Note:** Read-only or scoped-down SSO roles often lack these CloudWatch Logs Insights permissions. Use a role with broader permissions (e.g. `AWSPowerUserAccess`) or assume the Terraform deployment role.

### S3 Export for Athena (Historical)

OTEL spans are also exported to S3 via Firehose for long-term storage and Athena queries.

**S3 Bucket:** `medical-nudging-otel-spans-123456789012`

**Data Format:** The Firehose transformation Lambda flattens the CloudWatch envelope, outputting one OTEL span JSON per line (gzip compressed).

#### Athena Query Example

```sql
-- Query OTEL spans (requires Glue table setup)
SELECT 
  json_extract_scalar(line, '$.name') as span_name,
  json_extract_scalar(line, '$.traceId') as trace_id,
  json_extract_scalar(line, '$.attributes.session_id') as session_id
FROM otel_spans
WHERE year = '2026' AND month = '02' AND day = '03'
LIMIT 100;
```

### Programmatic Retrieval

The `run_inference.py` script automatically retrieves OTEL traces when using `--mode agentcore`:

```bash
uv run scripts/run_inference.py --mode agentcore --sample 1
# Traces saved to results/traces/inference_agentcore_YYYYMMDD_HHMMSS/
```

---

## AgentCore Runtime Logs

AgentCore runtimes emit application logs to CloudWatch. These logs include startup configuration, request processing, nudge generation results, and errors.

### Log Group Pattern

```
/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT
```

**Current Runtime:** `/aws/bedrock-agentcore/runtimes/YOUR_RUNTIME_ID-DEFAULT`

**Console:** [CloudWatch Logs](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws$252Fbedrock-agentcore$252Fruntimes$252FYOUR_RUNTIME_ID-DEFAULT)

### CloudWatch Logs Insights Queries

Navigate to [CloudWatch Logs Insights](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:logs-insights) and select the runtime log group.

#### Recent Nudge Generation Results
```
fields @timestamp, @message
| parse @message '"body":"*"' as body
| filter body like /Generated.*nudges/
| sort @timestamp desc
| limit 20
```

#### Errors and Warnings
```
fields @timestamp, @message
| parse @message '"severityText":"*"' as severity
| parse @message '"body":"*"' as body
| filter severity in ["WARN", "WARNING", "ERROR"]
| sort @timestamp desc
| limit 50
```

#### Request Processing (Start to Finish)
```
fields @timestamp, @message
| parse @message '"body":"*"' as body
| filter body like /request|Generated|starting|ready/
| sort @timestamp desc
| limit 30
```

#### Tool Calls
```
fields @timestamp, @message
| parse @message '"body":*' as body
| filter @message like /tool_use|toolUse|search_guidelines|calculate/
| sort @timestamp desc
| limit 20
```

#### Startup Configuration
```
fields @timestamp, @message
| parse @message '"body":"*"' as body
| filter body like /MODEL_ID|MAX_NUDGES|SEARCH_BACKEND|starting up|ready/
| sort @timestamp desc
| limit 20
```

#### S3 Trace Writes (Success/Failure)
```
fields @timestamp, @message
| parse @message '"body":"*"' as body
| filter body like /trace|S3|write_trace/
| sort @timestamp desc
| limit 20
```

#### All Logs (Raw)
```
fields @timestamp, @message
| sort @timestamp desc
| limit 100
```

### CLI Usage

```bash
# List available runtime log groups
aws logs describe-log-groups \
  --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/" \
  --region us-east-1 \
  --query 'logGroups[*].logGroupName'

# Get recent logs
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/YOUR_RUNTIME_ID-DEFAULT" \
  --start-time $(($(date +%s) - 3600))000 \
  --limit 20 \
  --region us-east-1

# Filter for nudge generation results
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/YOUR_RUNTIME_ID-DEFAULT" \
  --filter-pattern "Generated" \
  --start-time $(($(date +%s) - 3600))000 \
  --limit 10 \
  --region us-east-1

# Filter for errors
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/YOUR_RUNTIME_ID-DEFAULT" \
  --filter-pattern "?error ?ERROR ?Failed ?WARNING" \
  --start-time $(($(date +%s) - 3600))000 \
  --limit 20 \
  --region us-east-1
```

---

## Troubleshooting

### No metrics appearing

1. Check runtime has `METRICS_ENABLED=true` environment variable
2. Verify `AWS_REGION` is set in runtime environment
3. Check CloudWatch logs for metric emission errors:
   ```bash
   aws logs filter-log-events \
     --log-group-name "/aws/bedrock-agentcore/runtimes/YOUR_RUNTIME_ID-DEFAULT" \
     --filter-pattern "metric" \
     --region us-east-1
   ```

### Athena query returns no results

1. Verify partition projection is enabled on the Glue table
2. Check S3 bucket has events in the expected path structure
3. Ensure date filters match actual data dates

### Dashboard not loading

1. Verify IAM permissions include `cloudwatch:GetDashboard`
2. Check dashboard exists: `aws cloudwatch list-dashboards --region us-east-1`


---

## Transaction Search Setup

Transaction Search enables rich OTEL span data in CloudWatch for debugging agent executions. This is set up automatically by:
- **Terraform**: `cloudwatch.tf` creates the resource policy
- **deploy_agentcore.sh**: Step 3b creates the resource policy and enables Transaction Search

### Manual Setup (if needed)

If Transaction Search isn't working, you can enable it manually:

```bash
# 1. Create resource policy allowing X-Ray to write to aws/spans
aws logs put-resource-policy \
  --policy-name "TransactionSearchAccess" \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "TransactionSearchXRayAccess",
      "Effect": "Allow",
      "Principal": {"Service": "xray.amazonaws.com"},
      "Action": "logs:PutLogEvents",
      "Resource": [
        "arn:aws:logs:us-east-1:YOUR_ACCOUNT_ID:log-group:aws/spans:*",
        "arn:aws:logs:us-east-1:YOUR_ACCOUNT_ID:log-group:/aws/application-signals/data:*"
      ],
      "Condition": {
        "ArnLike": {"aws:SourceArn": "arn:aws:xray:us-east-1:YOUR_ACCOUNT_ID:*"},
        "StringEquals": {"aws:SourceAccount": "YOUR_ACCOUNT_ID"}
      }
    }]
  }'

# 2. Enable Transaction Search
aws xray update-trace-segment-destination --destination CloudWatchLogs

# 3. Verify
aws xray get-trace-segment-destination
# Expected: {"Destination": "CloudWatchLogs", "Status": "ACTIVE"}
```

### Console Alternative

You can also enable tracing from the AgentCore console:
1. Navigate to your AgentCore runtime in the AWS Console
2. Find the **Tracing** section
3. Click **Enable** to turn on Transaction Search

### Important: Don't Set OTEL Env Vars

For agents hosted on AgentCore Runtime, do NOT set these environment variables:
- `AWS_XRAY_SDK_ENABLED`
- `OTEL_TRACES_EXPORTER`
- `OTEL_PYTHON_DISTRO`
- `OTEL_PYTHON_CONFIGURATOR`

AgentCore automatically configures the OTEL pipeline. Setting these manually will break trace generation.
