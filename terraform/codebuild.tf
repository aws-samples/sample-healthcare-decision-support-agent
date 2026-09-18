# ============================================================================
# Medical Nudging AgentCore - CodeBuild Project
# ============================================================================

resource "aws_codebuild_project" "agent_image" {
  name          = "${var.stack_name}-agent-build"
  description   = "Build Medical Nudging agent Docker image (ARM64) for ${var.stack_name}"
  service_role  = aws_iam_role.image_build.arn
  build_timeout = 60

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type = "BUILD_GENERAL1_LARGE"
    image        = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"
    type         = "ARM_CONTAINER"
    # CKV_AWS_316: Privileged mode is required for Docker-in-Docker builds
    # This allows CodeBuild to build and push container images to ECR
    # Risk accepted: Necessary for containerized agent deployment pipeline
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "AWS_DEFAULT_REGION"
      value = local.region
    }

    environment_variable {
      name  = "AWS_ACCOUNT_ID"
      value = local.account_id
    }

    environment_variable {
      name  = "IMAGE_REPO_NAME"
      value = aws_ecr_repository.agent_ecr.name
    }

    environment_variable {
      name  = "IMAGE_TAG"
      value = local.effective_image_tag
    }

    environment_variable {
      name  = "STACK_NAME"
      value = var.stack_name
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.agent_source.id}/${aws_s3_object.agent_source.key}"
    buildspec = "buildspec.yml"
  }

  logs_config {
    cloudwatch_logs {
      group_name  = "/aws/codebuild/${var.stack_name}-agent-build"
      stream_name = "build-log"
    }
  }

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-agent-build"
    Module = "CodeBuild"
  })
}

# ============================================================================
# Trigger CodeBuild - Build Image Before Creating Runtime
# ============================================================================

# Trigger CodeBuild to build agent image
# Uses inline AWS CLI command for cross-platform Windows/Mac/Linux compatibility
resource "null_resource" "trigger_build" {
  triggers = {
    build_project = aws_codebuild_project.agent_image.id
    image_tag     = local.effective_image_tag
    # Trigger rebuild if ECR repository changes
    ecr_repository = aws_ecr_repository.agent_ecr.id
    # Trigger rebuild when source code changes (MD5 hash)
    source_code_md5 = data.external.agent_source_archive.result.md5
  }

  # Cross-platform: Python script starts build and waits for completion
  provisioner "local-exec" {
    command = "python ${path.module}/scripts/wait_for_codebuild.py --project-name ${aws_codebuild_project.agent_image.name} --region ${local.region}${var.aws_profile != null && var.aws_profile != "" ? " --profile ${var.aws_profile}" : ""}"
  }

  depends_on = [
    aws_codebuild_project.agent_image,
    aws_ecr_repository.agent_ecr,
    aws_iam_role_policy.image_build,
    aws_s3_object.agent_source
  ]
}

# ============================================================================
# CloudWatch Log Group for CodeBuild
# ============================================================================

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${var.stack_name}-agent-build"
  retention_in_days = var.log_retention_days

  # CKV_AWS_158: Add KMS encryption to CloudWatch log group
  kms_key_id = var.enable_kms_encryption ? aws_kms_key.cloudwatch[0].arn : null

  tags = merge(local.common_tags, {
    Name   = "${var.stack_name}-codebuild-logs"
    Module = "CodeBuild"
  })
}
