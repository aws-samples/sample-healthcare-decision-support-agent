# ============================================================================
# Medical Nudging - AWS HealthLake FHIR Datastore (optional)
# ============================================================================
#
# ⚠️  COST WARNING — READ BEFORE ENABLING
#
# A HealthLake datastore is billed from creation to deletion and CANNOT be
# paused or stopped. At the published us-east-1 rate of $0.27 per datastore-hour
# that is roughly:
#
#     $0.27 / hour   ≈   $6.48 / day   ≈   $197 / month
#
# ...for an *idle* datastore, before storage or query charges. The 100-patient
# MIMIC-IV demo fits inside the free 10 GB storage tier and the free
# 3,500 queries/hour allowance, so the monthly bill is essentially the
# "it exists" charge.
#
# This is the single largest cost in this sample. Do not leave a demo datastore
# running. Teardown:
#
#     terraform destroy -target=awscc_healthlake_fhir_datastore.fhir \
#                       -var="healthlake_enabled=true"
#
# See docs/fhir-api-integration.md for the full cost/teardown checklist.
#
# ----------------------------------------------------------------------------
# Provisioning notes
#
# * `hashicorp/aws` has NO HealthLake resource (the provider's own README says
#   so), hence the `awscc` (Cloud Control API) provider for this one resource.
# * Only `datastore_type_version = "R4"` and a name are set. Identity-provider
#   and SSE configuration are deliberately omitted because the API defaults are
#   already what this sample wants: AWS_AUTH (SigV4) with an AWS-owned KMS key.
#   Supplying `sse_configuration` with a CMK would additionally require every
#   FHIR API *caller* to hold KMS permissions.
# * Datastore creation took 2-3 minutes in testing (service docs say up to 30).
#   The caller also needs `ram:GetResourceShareInvitations`, or Cloud Control
#   reports the create as FAILED with an AccessDenied status message.
# * `awscc` has no per-resource
#   `timeouts` block; the provider's built-in Cloud Control wait for this
#   resource type is 120 minutes for both create and delete, which is generous
#   enough. Expect a slow first apply and do not batch it with fast-moving
#   resources.
# * Changing the name or type version REPLACES the datastore and destroys all
#   imported data.
# * A datastore must be created after 2023-12-09 for `_sort` on date fields to
#   work. A freshly created datastore satisfies this; never reuse an old one.
# ============================================================================

locals {
  healthlake_count         = var.healthlake_enabled ? 1 : 0
  healthlake_datastore_arn = var.healthlake_enabled ? awscc_healthlake_fhir_datastore.fhir[0].datastore_arn : ""
}

# ----------------------------------------------------------------------------
# Datastore
# ----------------------------------------------------------------------------

resource "awscc_healthlake_fhir_datastore" "fhir" {
  count = local.healthlake_count

  datastore_name         = "${var.stack_name}-${var.healthlake_datastore_name}"
  datastore_type_version = "R4"

  tags = [
    { key = "Name", value = "${var.stack_name}-${var.healthlake_datastore_name}" },
    { key = "Project", value = "MedicalNudging" },
    { key = "StackName", value = var.stack_name },
    { key = "Environment", value = var.environment },
    { key = "ManagedBy", value = "Terraform" },
  ]
}

# ----------------------------------------------------------------------------
# KMS key for the import job's output (JobOutputDataConfig requires KmsKeyId —
# there is no way to run an import job without one)
# ----------------------------------------------------------------------------

resource "aws_kms_key" "healthlake_import" {
  count = local.healthlake_count

  description             = "KMS key for HealthLake import job output - ${var.stack_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableIAMUserPermissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowHealthLakeImportRole"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.healthlake_import[0].arn }
        Action = [
          "kms:DescribeKey",
          "kms:GenerateDataKey*",
          "kms:Encrypt",
          "kms:Decrypt",
        ]
        Resource = "*"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-healthlake-import-kms"
  })
}

resource "aws_kms_alias" "healthlake_import" {
  count = local.healthlake_count

  name          = "alias/${var.stack_name}-healthlake-import"
  target_key_id = aws_kms_key.healthlake_import[0].key_id
}

# ----------------------------------------------------------------------------
# Staging bucket — `import/` holds the NDJSON to ingest, `output/` receives the
# job's Manifest.json plus SUCCESS/ (and FAILURE/, when there are failures).
# The docs recommend two buckets; one with two prefixes works and is simpler.
# ----------------------------------------------------------------------------

resource "aws_s3_bucket" "healthlake_staging" {
  count = local.healthlake_count

  bucket        = "${var.stack_name}-healthlake-${local.account_id}"
  force_destroy = true

  tags = merge(local.common_tags, {
    Name    = "${var.stack_name}-healthlake-staging"
    Purpose = "Stage NDJSON for HealthLake bulk import and collect job output"
    Module  = "HealthLake"
  })
}

resource "aws_s3_bucket_public_access_block" "healthlake_staging" {
  count = local.healthlake_count

  bucket = aws_s3_bucket.healthlake_staging[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "healthlake_staging" {
  count = local.healthlake_count

  bucket = aws_s3_bucket.healthlake_staging[0].id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.healthlake_import[0].arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "healthlake_staging" {
  count = local.healthlake_count

  bucket = aws_s3_bucket.healthlake_staging[0].id

  rule {
    id     = "expire-staged-ndjson"
    status = "Enabled"

    filter {
      prefix = "import/"
    }

    expiration {
      days = var.healthlake_staging_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ----------------------------------------------------------------------------
# Import data-access role — HealthLake assumes this to read the staged NDJSON
# and write the job report.
# ----------------------------------------------------------------------------

resource "aws_iam_role" "healthlake_import" {
  count = local.healthlake_count

  name        = "${var.stack_name}-healthlake-import"
  description = "Role HealthLake assumes to run FHIR bulk import jobs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "healthlake.amazonaws.com" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account_id }
          ArnEquals    = { "aws:SourceArn" = local.healthlake_datastore_arn }
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-healthlake-import"
  })
}

resource "aws_iam_role_policy" "healthlake_import" {
  count = local.healthlake_count

  name = "${var.stack_name}-healthlake-import"
  role = aws_iam_role.healthlake_import[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StagingBucketMetadata"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketPublicAccessBlock",
          "s3:GetEncryptionConfiguration",
        ]
        Resource = aws_s3_bucket.healthlake_staging[0].arn
      },
      {
        # s3:GetObject is not in the AWS docs' example policy, which cannot be
        # sufficient to read the input NDJSON. Verified necessary by the spike.
        Sid    = "StagingBucketObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.healthlake_staging[0].arn}/*"
      },
      {
        Sid    = "ImportOutputKms"
        Effect = "Allow"
        Action = [
          "kms:DescribeKey",
          "kms:GenerateDataKey*",
          "kms:Encrypt",
          "kms:Decrypt",
        ]
        Resource = aws_kms_key.healthlake_import[0].arn
      }
    ]
  })
}

# ----------------------------------------------------------------------------
# Budget guardrail — an idle datastore silently accrues ~$197/month, so the
# budget is on by default whenever HealthLake is enabled.
# ----------------------------------------------------------------------------

resource "aws_budgets_budget" "healthlake" {
  count = var.healthlake_enabled && var.healthlake_budget_alarm_enabled ? 1 : 0

  name         = "${var.stack_name}-healthlake-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.healthlake_monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # "Amazon HealthLake" is the service's billing display name, confirmed against
  # the Price List API (service code AmazonHealthLake -> servicename "Amazon
  # HealthLake"). AWS documentation uses "AWS HealthLake" in places; that string
  # matches nothing here and would make the budget silently track $0.
  cost_filter {
    name   = "Service"
    values = ["Amazon HealthLake"]
  }

  dynamic "notification" {
    for_each = var.healthlake_budget_alert_emails
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 80
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = [notification.value]
    }
  }

  dynamic "notification" {
    for_each = var.healthlake_budget_alert_emails
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 100
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [notification.value]
    }
  }
}
