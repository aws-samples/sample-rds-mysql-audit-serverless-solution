"""
Cluster configuration model for multi-cluster/instance audit log collection.

Defines the ClusterConfig data structure and provides functions for parsing,
validating, and serializing cluster configuration JSON files.

Config format (version "1.0"):
    {
        "version": "1.0",
        "generated_at": "2024-01-15T10:30:00Z",
        "region": "us-east-1",
        "clusters": [
            {
                "cluster_id": "my-cluster",
                "instance_ids": ["instance-1", "instance-2"],
                "enabled": true,
                "type": "aurora-mysql"
            }
        ]
    }
"""

import json
import re

SUPPORTED_VERSION = "1.0"

_SAFE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-]{0,62}$')

_REQUIRED_TOP_LEVEL_FIELDS = {
    "version": str,
    "generated_at": str,
    "region": str,
    "clusters": list,
}

_REQUIRED_CLUSTER_FIELDS = {
    "cluster_id": str,
    "instance_ids": list,
}


def parse_config(json_str: str) -> dict:
    """Parse a JSON string into a validated cluster config dict.

    Applies defaults (``enabled`` → ``True``) and validates structure.

    Args:
        json_str: Raw JSON string.

    Returns:
        Validated config dict.

    Raises:
        ValueError: If the JSON is malformed or the config is invalid.
    """
    try:
        config = json.loads(json_str)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("Config must be a JSON object")

    # Apply enabled default before validation
    _apply_defaults(config)
    validate_config(config)
    return config


def validate_config(config: dict) -> bool:
    """Validate a cluster config dict.

    Checks required top-level fields, version, and cluster entry structure.

    Args:
        config: Config dict to validate.

    Returns:
        ``True`` if valid.

    Raises:
        ValueError: If any validation check fails.
    """
    if not isinstance(config, dict):
        raise ValueError("Config must be a dict")

    # Top-level required fields
    for field, expected_type in _REQUIRED_TOP_LEVEL_FIELDS.items():
        if field not in config:
            raise ValueError(f"Missing required field: {field}")
        if not isinstance(config[field], expected_type):
            raise ValueError(
                f"Field '{field}' must be {expected_type.__name__}, "
                f"got {type(config[field]).__name__}"
            )

    # Version check
    if config["version"] != SUPPORTED_VERSION:
        raise ValueError(
            f"Unsupported version: {config['version']}, "
            f"expected {SUPPORTED_VERSION}"
        )

    # Validate each cluster entry
    for idx, cluster in enumerate(config["clusters"]):
        _validate_cluster_entry(cluster, idx)

    return True


def serialize_config(config: dict) -> str:
    """Serialize a config dict to a JSON string.

    Args:
        config: Validated config dict.

    Returns:
        JSON string with 2-space indentation.
    """
    return json.dumps(config, indent=2, ensure_ascii=False)


def _apply_defaults(config: dict) -> None:
    """Apply default values to cluster entries in-place.

    Sets ``enabled`` to ``True`` for any cluster entry missing the field.
    """
    clusters = config.get("clusters")
    if not isinstance(clusters, list):
        return
    for cluster in clusters:
        if isinstance(cluster, dict) and "enabled" not in cluster:
            cluster["enabled"] = True


def _validate_cluster_entry(cluster: dict, idx: int) -> None:
    """Validate a single cluster entry.

    Args:
        cluster: Cluster dict to validate.
        idx: Index in the clusters array (for error messages).

    Raises:
        ValueError: If the entry is invalid.
    """
    if not isinstance(cluster, dict):
        raise ValueError(f"clusters[{idx}] must be a dict")

    for field, expected_type in _REQUIRED_CLUSTER_FIELDS.items():
        if field not in cluster:
            raise ValueError(f"clusters[{idx}] missing required field: {field}")
        if not isinstance(cluster[field], expected_type):
            raise ValueError(
                f"clusters[{idx}].{field} must be {expected_type.__name__}, "
                f"got {type(cluster[field]).__name__}"
            )

    # cluster_id must be a safe RDS identifier
    cluster_id = cluster["cluster_id"]
    if not _SAFE_ID_RE.match(cluster_id):
        raise ValueError(
            f"clusters[{idx}].cluster_id contains unsafe characters: {cluster_id!r}. "
            "Only alphanumeric characters and hyphens are allowed."
        )

    # instance_ids must contain only safe RDS identifier strings
    for i, iid in enumerate(cluster["instance_ids"]):
        if not isinstance(iid, str):
            raise ValueError(
                f"clusters[{idx}].instance_ids[{i}] must be str, "
                f"got {type(iid).__name__}"
            )
        if not _SAFE_ID_RE.match(iid):
            raise ValueError(
                f"clusters[{idx}].instance_ids[{i}] contains unsafe characters: {iid!r}. "
                "Only alphanumeric characters and hyphens are allowed."
            )

    # enabled must be bool
    if "enabled" not in cluster:
        raise ValueError(f"clusters[{idx}] missing required field: enabled")
    if not isinstance(cluster["enabled"], bool):
        raise ValueError(
            f"clusters[{idx}].enabled must be bool, "
            f"got {type(cluster['enabled']).__name__}"
        )
