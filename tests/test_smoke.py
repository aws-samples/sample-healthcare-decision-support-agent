import os

import pytest


def test_imports() -> None:
    """Verify basic imports work."""
    from medical_nudging import __version__

    assert __version__ == "0.1.0"


@pytest.mark.skipif(not os.environ.get("AWS_ACCESS_KEY_ID"), reason="AWS credentials not available")
def test_bedrock_connectivity() -> None:
    """Placeholder test for Bedrock connectivity."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    assert client is not None
