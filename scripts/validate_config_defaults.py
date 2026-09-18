#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Validate that config defaults are aligned between settings.yaml.example and Terraform.

Usage:
    uv run scripts/validate_config_defaults.py
"""

import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent

# Mapping: (yaml_key, terraform_var, description)
CONFIG_MAPPINGS = [
    ("aws_region", "aws_region", "AWS region"),
    ("opensearch_index", "opensearch_index_name", "OpenSearch index name"),
    ("agent.search_backend", "search_backend", "Search backend type"),
    ("model.model_id", "model_id", "Bedrock model id"),
]

# settings.yaml.example keeps `auto` so zero-cost local runs fall back to ripgrep when no
# OpenSearch endpoint is configured; on a deployed runtime `auto` resolves to the Terraform
# default, so the pair is compatible rather than mismatched.
COMPATIBLE_VALUES = {"agent.search_backend": {("auto", "opensearch")}}


def load_yaml_defaults() -> dict:
    """Load defaults from settings.yaml.example."""
    yaml_path = PROJECT_ROOT / "config" / "settings.yaml.example"
    with open(yaml_path) as f:
        return yaml.safe_load(f) or {}


def load_terraform_defaults() -> dict:
    """Load defaults from terraform/variables.tf using regex."""
    tf_path = PROJECT_ROOT / "terraform" / "variables.tf"
    content = tf_path.read_text()

    defaults = {}
    # Match: variable "name" { ... default = "value" ... }
    var_pattern = r'variable\s+"(\w+)"\s*\{[^}]*default\s*=\s*"([^"]*)"'
    for match in re.finditer(var_pattern, content, re.DOTALL):
        defaults[match.group(1)] = match.group(2)

    return defaults


def get_nested_value(d: dict, key: str):
    """Get nested dict value using dot notation."""
    keys = key.split(".")
    value = d
    for k in keys:
        if isinstance(value, dict):
            value = value.get(k)
        else:
            return None
    return value


def main():
    yaml_defaults = load_yaml_defaults()
    tf_defaults = load_terraform_defaults()

    mismatches = []

    for yaml_key, tf_var, description in CONFIG_MAPPINGS:
        yaml_val = get_nested_value(yaml_defaults, yaml_key)
        tf_val = tf_defaults.get(tf_var)

        # Skip if yaml doesn't define a default (empty string or missing)
        if yaml_val in (None, ""):
            continue

        if (yaml_val, tf_val) in COMPATIBLE_VALUES.get(yaml_key, set()):
            continue

        if yaml_val != tf_val:
            mismatches.append(
                {
                    "setting": description,
                    "yaml_key": yaml_key,
                    "tf_var": tf_var,
                    "yaml_value": yaml_val,
                    "tf_value": tf_val,
                }
            )

    if mismatches:
        print("❌ Config default mismatches found:\n")
        for m in mismatches:
            print(f"  {m['setting']}:")
            print(f"    settings.yaml.example ({m['yaml_key']}): {m['yaml_value']}")
            print(f"    terraform/variables.tf ({m['tf_var']}): {m['tf_value']}")
            print()
        sys.exit(1)
    else:
        print("✅ All config defaults are aligned")
        sys.exit(0)


if __name__ == "__main__":
    main()
