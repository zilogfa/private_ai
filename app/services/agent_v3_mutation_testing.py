"""Deterministic Node mutation-testing lane for ATLAS v3 controlled fail→repair demos.

A user-requested fail→repair demonstration should not require a language model to
invent a bug.  Once a clean baseline is authoritatively verified, ATLAS creates
small syntax-preserving implementation mutants, executes each candidate inside
the ordinary staged sandbox, and promotes only a mutant that is proven to make
the previously-green test suite fail.

No candidate touches the durable workspace until a real failing preflight is
observed.  This keeps demonstration faults bounded, reproducible and independent
of local-model latency/intelligence.
"""

import re



class V3MutationError(Exception):
    pass


_MAX_MUTATION_TRIALS = 12


def _replace_once(source, start, end, replacement):
    return source[:start] + replacement + source[end:]


def _candidate(operator, source, start, end, replacement, detail):
    mutated = _replace_once(source, start, end, replacement)
    if mutated == source:
        return None
    return {
        "operator": operator,
        "content": mutated,
        "detail": detail,
    }


def _guard_mutations(source):
    """Yield small branch mutations while preserving JavaScript syntax.

    These are deliberately conservative textual operators.  They are not used
    to repair code or infer behavior; staged execution decides whether a mutant
    actually produces the requested failing demonstration.
    """
    candidates = []

    # `if (!expr) {` -> `if (expr) {` for common simple guard expressions.
    # Keep the grammar intentionally narrow so nested/multiline expressions do
    # not become unsafe text surgery.
    negated = re.compile(
        r"\bif\s*\(\s*!\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\([^()]*\))?)\s*\)"
    )
    for match in negated.finditer(source):
        expression = match.group(1)
        replacement = f"if ({expression})"
        item = _candidate(
            "negate_guard",
            source,
            match.start(),
            match.end(),
            replacement,
            "Invert one negated implementation guard condition.",
        )
        if item:
            candidates.append(item)

    # Strict equality mutations are classic mutation-testing operators and do
    # not alter public shape/imports/dependencies.
    for token, replacement, operator in (
        ("===", "!==", "flip_strict_equality"),
        ("!==", "===", "flip_strict_inequality"),
    ):
        for match in re.finditer(re.escape(token), source):
            item = _candidate(
                operator,
                source,
                match.start(),
                match.end(),
                replacement,
                f"Replace one {token} comparison with {replacement}.",
            )
            if item:
                candidates.append(item)

    # Boolean literal flips are safe, localized mutations.
    for token, replacement in (("true", "false"), ("false", "true")):
        for match in re.finditer(r"\b" + token + r"\b", source):
            item = _candidate(
                "flip_boolean_literal",
                source,
                match.start(),
                match.end(),
                replacement,
                f"Flip one boolean literal from {token} to {replacement}.",
            )
            if item:
                candidates.append(item)

    # A common predicate form in array callbacks / domain lookups.  This is
    # intentionally after equality/guard operators so simpler mutants win.
    for token, replacement, operator in (
        (".includes(", ".__atlas_missing_includes__(", "break_includes_predicate"),
    ):
        for match in re.finditer(re.escape(token), source):
            # This operator would change API shape at runtime and can produce a
            # TypeError rather than a semantic failure; omit it from selection.
            # Kept as documentation of a consciously rejected mutation class.
            _ = (match, replacement, operator)

    # Dedupe by complete candidate source and keep the trial set bounded.
    result = []
    seen = set()
    for item in candidates:
        content = item["content"]
        if content in seen:
            continue
        seen.add(content)
        result.append(item)
        if len(result) >= _MAX_MUTATION_TRIALS:
            break
    return result


def generate_node_mutants(source):
    return _guard_mutations(str(source or ""))


def is_legitimate_failing_execution(execution):
    if not isinstance(execution, dict):
        return False
    if str(execution.get("status") or "") != "failed":
        return False
    try:
        if int(execution.get("exit_code")) == 0:
            return False
    except Exception:
        pass

    evidence = "\n".join([
        str(execution.get("stdout") or ""),
        str(execution.get("stderr") or ""),
    ]).lower()
    # A controlled demonstration should exercise the test suite, not merely
    # create syntactically invalid code or a module-loader failure.
    forbidden = (
        "syntaxerror",
        "cannot determine intended module format",
        "unexpected token",
    )
    if any(marker in evidence for marker in forbidden):
        return False
    return True


def select_failing_node_mutant(run, target_filename, baseline_execution):
    """Return one staged/proven failing mutation without mutating the workspace."""
    # Lazy imports keep the pure mutation generator usable in lightweight tests
    # and avoid coupling operator generation to storage/runtime initialization.
    from app.services.agent_sandbox import read_workspace_file
    from app.services.agent_v3_candidate_pipeline import V3CandidateError, validate_candidate
    if not baseline_execution or str(baseline_execution.get("status") or "") != "success":
        raise V3MutationError(
            "Controlled mutation testing requires a previously verified green baseline."
        )

    source = read_workspace_file(
        run["user_id"],
        run["id"],
        target_filename,
        max_chars=220000,
    )
    mutants = generate_node_mutants(source)
    if not mutants:
        raise V3MutationError(
            f"No safe deterministic mutation operators matched {target_filename}."
        )

    trials = []
    for number, mutant in enumerate(mutants, start=1):
        changes = [{
            "filename": target_filename,
            "content": mutant["content"],
            "reason": (
                "User-requested controlled fail→repair demonstration via deterministic "
                f"mutation operator {mutant['operator']}."
            ),
        }]
        try:
            preflight = validate_candidate(
                run,
                changes,
                baseline_execution=None,
                purpose=f"intentional_defect:{mutant['operator']}",
            )
        except V3CandidateError as error:
            trials.append({
                "trial": number,
                "operator": mutant["operator"],
                "status": "candidate_rejected",
                "detail": str(error)[:1200],
            })
            continue

        execution = preflight.get("execution") or {}
        trial = {
            "trial": number,
            "operator": mutant["operator"],
            "status": str(execution.get("status") or "unknown"),
            "exit_code": execution.get("exit_code"),
            "duration_ms": int(execution.get("duration_ms") or 0),
            "detail": mutant["detail"],
        }
        trials.append(trial)

        if is_legitimate_failing_execution(execution):
            return {
                "lane": "deterministic_mutation_testing",
                "model": "deterministic",
                "operator": mutant["operator"],
                "detail": mutant["detail"],
                "files": changes,
                "preflight": preflight,
                "trials": trials,
            }

    summary = "; ".join(
        f"{item.get('operator')}={item.get('status')}"
        for item in trials[-6:]
    )
    raise V3MutationError(
        f"No bounded deterministic mutant for {target_filename} produced a legitimate failing test preflight"
        + (f" ({summary})." if summary else ".")
    )
