"""Explicit post-hoc exception, limited to Russia's final common population."""
import hashlib
import json
from pathlib import Path


def validate_exception(policy, country):
    if (country != "Russia" or policy.get("country") != "Russia"
            or policy.get("stage") != "master_common_population"
            or policy.get("minimum_area_fraction") != .91
            or policy.get("default_minimum_area_fraction") != .95
            or policy.get("authorization") != "user_explicit_2026-10-09"
            or not policy.get("reason")):
        raise ValueError("Invalid or out-of-scope Russia coverage exception")
    return .91


def load_exception(path, country, manifest, results_root):
    policy = json.loads(Path(path).read_text())
    minimum = validate_exception(policy, country)
    if (Path(policy["manifest"]).resolve() != manifest.resolve()
            or policy["manifest_sha256"] != hashlib.sha256(manifest.read_bytes()).hexdigest()
            or Path(policy["results_root"]).resolve() != results_root.resolve()):
        raise ValueError("Coverage exception belongs to a different campaign/manifest")
    return minimum, policy
