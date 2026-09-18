# ============================================================================
# OpenSearch Serverless Collection for Guidelines Search
# ============================================================================

# -----------------------------------------------------------------------------
# Encryption Policy (required before collection)
# -----------------------------------------------------------------------------

resource "aws_opensearchserverless_security_policy" "guidelines_encryption" {
  count = var.opensearch_enabled ? 1 : 0

  name = "${var.stack_name}-encryption"
  type = "encryption"

  policy = jsonencode({
    Rules = [
      {
        Resource     = ["collection/${var.stack_name}-guidelines"]
        ResourceType = "collection"
      }
    ]
    AWSOwnedKey = true
  })
}

# -----------------------------------------------------------------------------
# Network Policy - AOSS2/AOSS3: Configurable public access
# -----------------------------------------------------------------------------

resource "aws_opensearchserverless_security_policy" "guidelines_network" {
  count = var.opensearch_enabled ? 1 : 0

  name = "${var.stack_name}-network"
  type = "network"

  policy = jsonencode([
    {
      Rules = [
        {
          Resource     = ["collection/${var.stack_name}-guidelines"]
          ResourceType = "collection"
        }
      ]
      # AOSS2/AOSS3: Make public access configurable (set to false for production)
      AllowFromPublic = var.opensearch_allow_public
    }
  ])
}

# -----------------------------------------------------------------------------
# Data Access Policy
# -----------------------------------------------------------------------------

resource "aws_opensearchserverless_access_policy" "guidelines_data" {
  count = var.opensearch_enabled ? 1 : 0

  name = "${var.stack_name}-data-access"
  type = "data"

  policy = jsonencode([
    {
      Rules = [
        {
          Resource = ["collection/${var.stack_name}-guidelines"]
          Permission = [
            "aoss:CreateCollectionItems",
            "aoss:DeleteCollectionItems",
            "aoss:UpdateCollectionItems",
            "aoss:DescribeCollectionItems"
          ]
          ResourceType = "collection"
        },
        {
          Resource = ["index/${var.stack_name}-guidelines/*"]
          Permission = [
            "aoss:CreateIndex",
            "aoss:DeleteIndex",
            "aoss:UpdateIndex",
            "aoss:DescribeIndex",
            "aoss:ReadDocument",
            "aoss:WriteDocument"
          ]
          ResourceType = "index"
        }
      ]
      Principal = [
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      ]
    }
  ])
}

# -----------------------------------------------------------------------------
# OpenSearch Serverless Collection
# -----------------------------------------------------------------------------

resource "aws_opensearchserverless_collection" "guidelines" {
  count = var.opensearch_enabled ? 1 : 0

  name             = "${var.stack_name}-guidelines"
  type             = "SEARCH"
  standby_replicas = var.opensearch_standby_replicas

  depends_on = [
    aws_opensearchserverless_security_policy.guidelines_encryption,
    aws_opensearchserverless_security_policy.guidelines_network,
    aws_opensearchserverless_access_policy.guidelines_data
  ]

  tags = merge(local.common_tags, {
    Name = "${var.stack_name}-guidelines"
  })
}
