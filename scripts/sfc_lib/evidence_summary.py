"""Public facts projected from already validated supervisor check evidence."""
from __future__ import annotations

from .common import Halt


def verification_check_provenance(check):
    if not isinstance(check, dict) or type(check.get("exit_code")) is not int or check["exit_code"] != 0:
        raise Halt("Invalid passing check for public evidence summary")
    if "reuse" in check:
        source = check["reuse"]
        if (not isinstance(source, dict) or source.get("kind") != "execution" or
                type(source.get("exit_code")) is not int or source["exit_code"] != 0):
            raise Halt("Invalid reused check for public evidence summary")
        return "reused"
    if check.get("kind") != "execution":
        raise Halt("Invalid executed check for public evidence summary")
    return "executed"


def verification_check_counts(evidence):
    """Use the receipt validated by the caller. Never read logs or rerun checks."""
    if (not isinstance(evidence, dict) or evidence.get("kind") != "verification" or
            evidence.get("passed") is not True or not isinstance(evidence.get("checks"), list) or
            not evidence["checks"]):
        raise Halt("Invalid verification receipt for public evidence summary")
    checks = evidence["checks"]
    reused = sum(verification_check_provenance(check) == "reused" for check in checks)
    return len(checks) - reused, reused


def render_verification_check_counts(counts):
    if (not isinstance(counts, tuple) or len(counts) != 2 or
            any(type(value) is not int or not 0 <= value <= 10**12 for value in counts) or
            not sum(counts)):
        raise Halt("Invalid public verification counts")
    executed, reused = counts
    return f"Project verification: {executed} executed, {reused} reused."
