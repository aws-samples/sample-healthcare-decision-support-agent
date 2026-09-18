# ============================================================================
# Medical Nudging - Athena Analytics for Observability Events
# ============================================================================
# Creates Glue database and table for querying observability events in S3.
# Uses partition projection for automatic partition discovery.

# -----------------------------------------------------------------------------
# Glue Database
# -----------------------------------------------------------------------------

resource "aws_glue_catalog_database" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = replace("${var.stack_name}_observability", "-", "_")
  description = "Database for Medical Nudging observability events"

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-database"
  })
}

# -----------------------------------------------------------------------------
# Glue Table with Partition Projection
# -----------------------------------------------------------------------------

resource "aws_glue_catalog_table" "observability_events" {
  count = var.enable_datalake_observability ? 1 : 0

  name          = "events"
  database_name = aws_glue_catalog_database.observability[0].name
  table_type    = "EXTERNAL_TABLE"
  description   = "Observability events from Medical Nudging pipeline"

  parameters = {
    # Enable partition projection for automatic partition discovery
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2024,2030"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "projection.day.type"       = "integer"
    "projection.day.range"      = "1,31"
    "projection.day.digits"     = "2"
    "projection.hour.type"      = "integer"
    "projection.hour.range"     = "0,23"
    "projection.hour.digits"    = "2"
    "storage.location.template" = "s3://${aws_s3_bucket.observability[0].id}/events/year=$${year}/month=$${month}/day=$${day}/hour=$${hour}"
    "classification"            = "json"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.observability[0].id}/events/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      name                  = "json-serde"
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "serialization.format"  = "1"
        "ignore.malformed.json" = "true"
      }
    }

    # Event schema columns - must match actual JSON structure
    columns {
      name    = "version"
      type    = "string"
      comment = "Event schema version"
    }
    columns {
      name    = "event_id"
      type    = "string"
      comment = "Unique event identifier (UUID)"
    }
    columns {
      name    = "event_type"
      type    = "string"
      comment = "Event type (request_received, processing_started, tool_started, response_sent, error, api_error)"
    }
    columns {
      name    = "timestamp"
      type    = "string"
      comment = "ISO 8601 timestamp"
    }
    columns {
      name    = "correlation"
      type    = "struct<request_id:string,session_id:string,trace_id:string>"
      comment = "Correlation identifiers for request tracing"
    }
    columns {
      name    = "source"
      type    = "struct<component:string,mode:string,environment:string>"
      comment = "Source information (component, mode, environment)"
    }
    columns {
      name    = "payload"
      type    = "string"
      comment = "JSON payload with event-specific data"
    }
    columns {
      name    = "metadata"
      type    = "struct<stack_name:string,region:string>"
      comment = "Event metadata (stack_name, region)"
    }
  }

  # Partition keys match the S3 path structure
  partition_keys {
    name    = "year"
    type    = "string"
    comment = "Event year (YYYY)"
  }
  partition_keys {
    name    = "month"
    type    = "string"
    comment = "Event month (01-12)"
  }
  partition_keys {
    name    = "day"
    type    = "string"
    comment = "Event day (01-31)"
  }
  partition_keys {
    name    = "hour"
    type    = "string"
    comment = "Event hour (00-23)"
  }
}

# -----------------------------------------------------------------------------
# Athena Workgroup
# -----------------------------------------------------------------------------

resource "aws_athena_workgroup" "observability" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "${var.stack_name}-observability"
  description = "Workgroup for Medical Nudging observability queries"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    result_configuration {
      output_location = "s3://${aws_s3_bucket.observability[0].id}/athena-results/"

      encryption_configuration {
        encryption_option = var.enable_kms_encryption ? "SSE_KMS" : "SSE_S3"
        kms_key_arn       = var.enable_kms_encryption ? aws_kms_key.s3[0].arn : null
      }
    }
  }

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-observability-workgroup"
  })
}

# -----------------------------------------------------------------------------
# Named Queries for Common Analytics
# -----------------------------------------------------------------------------

resource "aws_athena_named_query" "event_counts_by_type" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "Event Counts by Type"
  description = "Count events by type for the last 24 hours"
  workgroup   = aws_athena_workgroup.observability[0].name
  database    = aws_glue_catalog_database.observability[0].name

  query = <<-EOQ
    SELECT
      event_type,
      COUNT(*) as event_count
    FROM events
    WHERE year = CAST(year(current_date) AS VARCHAR)
      AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
      AND day = LPAD(CAST(day(current_date) AS VARCHAR), 2, '0')
    GROUP BY event_type
    ORDER BY event_count DESC
  EOQ
}

resource "aws_athena_named_query" "error_summary" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "Error Summary"
  description = "Summary of errors in the last 24 hours"
  workgroup   = aws_athena_workgroup.observability[0].name
  database    = aws_glue_catalog_database.observability[0].name

  query = <<-EOQ
    SELECT
      event_type,
      json_extract_scalar(payload, '$.error_type') as error_type,
      json_extract_scalar(payload, '$.error_message') as error_message,
      COUNT(*) as error_count
    FROM events
    WHERE event_type IN ('error', 'api_error')
      AND year = CAST(year(current_date) AS VARCHAR)
      AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
      AND day = LPAD(CAST(day(current_date) AS VARCHAR), 2, '0')
    GROUP BY
      event_type,
      json_extract_scalar(payload, '$.error_type'),
      json_extract_scalar(payload, '$.error_message')
    ORDER BY error_count DESC
  EOQ
}

resource "aws_athena_named_query" "tool_performance" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "Tool Usage"
  description = "Tool call counts (note: duration not available in tool_started events)"
  workgroup   = aws_athena_workgroup.observability[0].name
  database    = aws_glue_catalog_database.observability[0].name

  query = <<-EOQ
    SELECT
      json_extract_scalar(payload, '$.tool_name') as tool_name,
      COUNT(*) as call_count
    FROM events
    WHERE event_type = 'tool_started'
      AND year = CAST(year(current_date) AS VARCHAR)
      AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
      AND day = LPAD(CAST(day(current_date) AS VARCHAR), 2, '0')
    GROUP BY json_extract_scalar(payload, '$.tool_name')
    ORDER BY call_count DESC
  EOQ
}

resource "aws_athena_named_query" "request_latency" {
  count = var.enable_datalake_observability ? 1 : 0

  name        = "Request Latency Distribution"
  description = "Request latency percentiles"
  workgroup   = aws_athena_workgroup.observability[0].name
  database    = aws_glue_catalog_database.observability[0].name

  query = <<-EOQ
    SELECT
      json_extract_scalar(payload, '$.status') as status,
      COUNT(*) as request_count,
      AVG(CAST(json_extract_scalar(payload, '$.processing_time_ms') AS DOUBLE)) as avg_latency_ms,
      APPROX_PERCENTILE(CAST(json_extract_scalar(payload, '$.processing_time_ms') AS DOUBLE), 0.50) as p50_latency_ms,
      APPROX_PERCENTILE(CAST(json_extract_scalar(payload, '$.processing_time_ms') AS DOUBLE), 0.95) as p95_latency_ms,
      APPROX_PERCENTILE(CAST(json_extract_scalar(payload, '$.processing_time_ms') AS DOUBLE), 0.99) as p99_latency_ms,
      AVG(CAST(json_extract_scalar(payload, '$.nudge_count') AS DOUBLE)) as avg_nudges
    FROM events
    WHERE event_type = 'response_sent'
      AND year = CAST(year(current_date) AS VARCHAR)
      AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
      AND day = LPAD(CAST(day(current_date) AS VARCHAR), 2, '0')
    GROUP BY json_extract_scalar(payload, '$.status')
  EOQ
}
