"""ATLAS v2.4.2 deterministic verified completion.

A verified workspace must never require another LLM call merely to say that it
passed. This module builds a compact terminal result directly from sandbox and
acceptance evidence, avoiding context-window failures after successful work.
"""


def build_verified_success_answer(run, *, evidence=None, acceptance=None):
    evidence = dict(evidence or {})
    acceptance = dict(acceptance or {})

    label = str(evidence.get("verification_label") or "sandbox verification")
    tests = evidence.get("test_count")
    passed = evidence.get("pass_count")
    failed = evidence.get("fail_count")

    lines = [
        "VERIFIED — The current workspace passed sandbox validation and the goal-level acceptance contract.",
        "",
        f"Verification: {label}",
    ]

    if isinstance(tests, int):
        detail = f"Tests: {tests}"
        if isinstance(passed, int):
            detail += f", passed: {passed}"
        if isinstance(failed, int):
            detail += f", failed: {failed}"
        lines.append(detail)

    if acceptance.get("satisfied") is True:
        lines.append("Acceptance contract: satisfied")

    lines.extend([
        "",
        "ATLAS finalized this result deterministically from authoritative execution evidence; no additional model synthesis was required.",
    ])

    return "\n".join(lines)[:6000]
