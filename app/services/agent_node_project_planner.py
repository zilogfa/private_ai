"""
ATLAS v2.3.1a - Deterministic Node/JavaScript Project Contract + Debug Planner.

This is the Node/JavaScript analyzer/planner implementation behind the shared
Project Intelligence facade. It keeps project debugging explicit and persistent
for smaller local models:

    source/test/package contract
        -> failure fingerprint/progress
        -> persistent repair plan
        -> one-file repair
        -> deterministic re-test

It never executes host code. JavaScript/Node source is inspected statically and
sandbox execution evidence remains authoritative.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from app.database import get_connection
from app.services import agent_runner as base_runner
from app.services.agents import (
    list_agent_steps,
    utc_iso,
)
from app.services.agent_sandbox import (
    list_agent_sandbox_executions,
    list_npm_scripts,
    list_workspace_files,
    read_workspace_file,
    sandbox_runtime_profile,
    write_workspace_file,
)
from app.services.agent_acceptance_contract import (
    acceptance_summary,
    evaluate_acceptance_contract,
    get_or_create_acceptance_contract,
    is_test_file,
    validate_test_candidate,
)
from app.services.agent_project_planner import (
    AGENT_EXPERT_MODEL,
    AGENT_REASONING_MODEL,
    AGENT_WORKER_MODEL,
    AUTO_ESCALATION_ENABLED,
    get_active_debug_plan,
    get_next_project_repair,
    initialize_agent_project_planner_storage,
    mark_active_plan_exhausted,
    mark_active_plan_resolved,
    mark_active_plan_superseded,
)


PROJECT_SOURCE_BUDGET = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_NODE_PROJECT_SOURCE_BUDGET",
        "22000",
    )
)
PROJECT_FILE_BUDGET = int(
    os.environ.get(
        "PRIVATE_AI_AGENT_NODE_PROJECT_FILE_BUDGET",
        "7000",
    )
)


class AgentNodeProjectPlannerError(Exception):
    pass


_JS_SOURCE_SUFFIXES = (
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
)


def _safe_json(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
    except Exception:
        return {} if default is None else default
    return parsed


def _normalize_module_specifier(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("."):
        return text
    return text.split("/")[0] if not text.startswith("@") else "/".join(text.split("/")[:2])


def _local_candidates(filename, specifier):
    if not str(specifier or "").startswith("."):
        return []

    source = Path(filename)
    base = (source.parent / specifier).as_posix()
    names = [base]

    if not Path(base).suffix:
        for suffix in _JS_SOURCE_SUFFIXES:
            names.append(base + suffix)
        for suffix in _JS_SOURCE_SUFFIXES:
            names.append((Path(base) / ("index" + suffix)).as_posix())

    normalized = []
    for item in names:
        value = str(Path(item)).replace("\\", "/")
        if value.startswith("./"):
            value = value[2:]
        normalized.append(value)
    return normalized


def _strip_js_comments_and_strings(source):
    """
    Mask comments and quoted/template string bodies while preserving length and
    newlines. The lightweight analyzer can then use balanced-brace scans without
    being confused by braces inside comments/strings.
    """
    text = str(source or "")
    out = list(text)
    i = 0
    n = len(text)
    state = "code"
    quote = None

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if state == "code":
            if ch == "/" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "line_comment"
                continue
            if ch == "/" and nxt == "*":
                out[i] = out[i + 1] = " "
                i += 2
                state = "block_comment"
                continue
            if ch in ("'", '"', "`"):
                quote = ch
                out[i] = " "
                i += 1
                state = "string"
                continue
            i += 1
            continue

        if state == "line_comment":
            if ch == "\n":
                state = "code"
            else:
                out[i] = " "
            i += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "code"
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue

        if state == "string":
            if ch == "\\":
                out[i] = " "
                if i + 1 < n:
                    if text[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
            if ch == quote:
                out[i] = " "
                i += 1
                state = "code"
                quote = None
                continue
            if ch != "\n":
                out[i] = " "
            i += 1

    return "".join(out)


def _matching_brace(masked, open_index):
    depth = 0
    for index in range(open_index, len(masked)):
        ch = masked[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _matching_paren(masked, open_index):
    depth = 0
    for index in range(open_index, len(masked)):
        ch = masked[index]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _line_number(source, offset):
    return str(source or "")[: max(0, int(offset))].count("\n") + 1


def _split_top_level_csv(text):
    value = str(text or "")
    if not value.strip():
        return []

    items = []
    start = 0
    stack = []
    quote = None
    escape = False

    for i, ch in enumerate(value):
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            continue

        if ch in "([{":
            stack.append(ch)
            continue
        if ch in ")]}":
            if stack:
                stack.pop()
            continue
        if ch == "," and not stack:
            items.append(value[start:i].strip())
            start = i + 1

    items.append(value[start:].strip())
    return [item for item in items if item]


def _signature_from_param_text(params):
    raw = _split_top_level_csv(params)
    required = 0
    names = []
    rest = False

    for item in raw:
        token = item.strip()
        if not token:
            continue

        if token.startswith("..."):
            rest = True
            token = token[3:].strip()

        has_default = "=" in token
        token = token.split("=", 1)[0].strip()
        token = re.sub(r"^\{|\}$", "", token).strip()
        token = re.sub(r"^\[|\]$", "", token).strip()
        token = re.sub(r"\s*:\s*[^,]+$", "", token).strip()  # TypeScript annotation
        token = re.sub(r"[?]$", "", token).strip()

        if token:
            names.append(token)
            if not has_default and not rest:
                required += 1

    return {
        "parameters": names,
        "required_positional": required,
        "max_positional": None if rest else len(raw),
        "rest": rest,
        "display": "(" + ", ".join(raw) + ")",
    }


def _extract_class_methods(source, masked, class_match):
    class_name = class_match.group(1)
    open_index = masked.find("{", class_match.end())
    if open_index < 0:
        return class_name, {}, None

    close_index = _matching_brace(masked, open_index)
    if close_index is None:
        return class_name, {}, None

    body_masked = masked[open_index + 1:close_index]
    body_source = source[open_index + 1:close_index]
    methods = {}

    method_re = re.compile(
        r"(?m)^\s*(?:static\s+)?(?:async\s+)?"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^)]*)\)\s*\{"
    )

    reserved_control_words = {
        "if", "for", "while", "switch", "catch", "with", "else", "do", "try",
    }
    for match in method_re.finditer(body_masked):
        name = match.group(1)
        if name in reserved_control_words:
            continue
        params = body_source[match.start(2):match.end(2)]
        methods[name] = _signature_from_param_text(params)

    constructor = methods.get(
        "constructor",
        {
            "parameters": [],
            "required_positional": 0,
            "max_positional": 0,
            "rest": False,
            "display": "()",
        },
    )

    return (
        class_name,
        {
            "methods": methods,
            "constructor": constructor,
            "line": _line_number(source, class_match.start()),
        },
        close_index,
    )


def _analyze_js_file(filename, source):
    source = str(source or "")
    masked = _strip_js_comments_and_strings(source)

    item = {
        "filename": filename,
        "module": Path(filename).stem,
        "parse_error": None,
        "imports": [],
        "exports": [],
        "functions": {},
        "classes": {},
        "instance_types": {},
        "calls": [],
        "tests": [],
        "assertions": [],
    }

    # ESM imports.
    import_from_re = re.compile(
        r"(?m)^\s*import\s+(.+?)\s+from\s+['\"]([^'\"]+)['\"]\s*;?"
    )
    for match in import_from_re.finditer(source):
        clause = match.group(1).strip()
        spec = match.group(2).strip()
        imported = []

        if clause.startswith("{") and "}" in clause:
            inner = clause[1:clause.rfind("}")]
            for part in _split_top_level_csv(inner):
                pieces = re.split(r"\s+as\s+", part.strip(), maxsplit=1)
                imported.append(
                    {
                        "symbol": pieces[0].strip(),
                        "alias": pieces[-1].strip(),
                    }
                )
        elif clause.startswith("* as "):
            imported.append(
                {
                    "symbol": "*",
                    "alias": clause[5:].strip(),
                }
            )
        else:
            default_name = clause.split(",", 1)[0].strip()
            if default_name:
                imported.append(
                    {
                        "symbol": "default",
                        "alias": default_name,
                    }
                )

        item["imports"].append(
            {
                "kind": "esm",
                "specifier": spec,
                "normalized": _normalize_module_specifier(spec),
                "symbols": imported,
                "line": _line_number(source, match.start()),
            }
        )

    side_effect_re = re.compile(
        r"(?m)^\s*import\s+['\"]([^'\"]+)['\"]\s*;?"
    )
    for match in side_effect_re.finditer(source):
        spec = match.group(1).strip()
        item["imports"].append(
            {
                "kind": "esm_side_effect",
                "specifier": spec,
                "normalized": _normalize_module_specifier(spec),
                "symbols": [],
                "line": _line_number(source, match.start()),
            }
        )

    # CommonJS imports.
    require_destructure = re.compile(
        r"\b(?:const|let|var)\s+\{([^}]+)\}\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)"
    )
    for match in require_destructure.finditer(source):
        symbols = []
        for part in _split_top_level_csv(match.group(1)):
            pieces = [x.strip() for x in part.split(":", 1)]
            symbols.append(
                {
                    "symbol": pieces[0],
                    "alias": pieces[-1],
                }
            )
        spec = match.group(2).strip()
        item["imports"].append(
            {
                "kind": "commonjs",
                "specifier": spec,
                "normalized": _normalize_module_specifier(spec),
                "symbols": symbols,
                "line": _line_number(source, match.start()),
            }
        )

    require_default = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)"
    )
    for match in require_default.finditer(source):
        spec = match.group(2).strip()
        item["imports"].append(
            {
                "kind": "commonjs",
                "specifier": spec,
                "normalized": _normalize_module_specifier(spec),
                "symbols": [
                    {
                        "symbol": "default",
                        "alias": match.group(1),
                    }
                ],
                "line": _line_number(source, match.start()),
            }
        )

    # Function declarations.
    function_re = re.compile(
        r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^)]*)\)"
    )
    for match in function_re.finditer(source):
        item["functions"][match.group(1)] = _signature_from_param_text(match.group(2))

    arrow_re = re.compile(
        r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>"
    )
    for match in arrow_re.finditer(source):
        item["functions"].setdefault(
            match.group(1),
            _signature_from_param_text(match.group(2)),
        )

    # Classes and method APIs.
    class_re = re.compile(
        r"\b(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"
    )
    for match in class_re.finditer(masked):
        name, info, _ = _extract_class_methods(source, masked, match)
        if info:
            item["classes"][name] = info

    # Exports.
    for match in re.finditer(
        r"(?m)^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        source,
    ):
        item["exports"].append(
            {
                "symbol": match.group(1),
                "line": _line_number(source, match.start()),
            }
        )

    for match in re.finditer(
        r"(?m)^\s*export\s*\{([^}]+)\}",
        source,
    ):
        for part in _split_top_level_csv(match.group(1)):
            pieces = re.split(r"\s+as\s+", part.strip(), maxsplit=1)
            item["exports"].append(
                {
                    "symbol": pieces[-1].strip(),
                    "line": _line_number(source, match.start()),
                }
            )

    module_object = re.compile(
        r"module\.exports\s*=\s*\{([^}]*)\}",
        re.S,
    )
    for match in module_object.finditer(source):
        for part in _split_top_level_csv(match.group(1)):
            key = part.split(":", 1)[0].strip()
            if key:
                item["exports"].append(
                    {
                        "symbol": key,
                        "line": _line_number(source, match.start()),
                    }
                )

    module_single = re.compile(
        r"module\.exports\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)"
    )
    for match in module_single.finditer(source):
        item["exports"].append(
            {
                "symbol": "default",
                "target": match.group(1),
                "line": _line_number(source, match.start()),
            }
        )

    for match in re.finditer(
        r"(?:^|\n)\s*exports\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=",
        source,
    ):
        item["exports"].append(
            {
                "symbol": match.group(1),
                "line": _line_number(source, match.start()),
            }
        )

    # Instance inference for common generated code.
    new_re = re.compile(
        r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*new\s+"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
    )
    for match in new_re.finditer(source):
        item["instance_types"][match.group(1)] = match.group(2)

    # Test names and assertion-bearing lines form part of the project contract.
    test_re = re.compile(
        r"\b(?:test|it)\s*\(\s*['\"]([^'\"]+)['\"]"
    )
    for match in test_re.finditer(source):
        item["tests"].append(
            {
                "name": match.group(1),
                "line": _line_number(source, match.start()),
            }
        )

    for line_no, line in enumerate(source.splitlines(), start=1):
        if re.search(
            r"\b(?:assert(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?|expect)\s*\(",
            line,
        ):
            item["assertions"].append(
                {
                    "line": line_no,
                    "text": line.strip()[:700],
                }
            )

    # Constructor and instance-method call arity.
    new_call_re = re.compile(
        r"\bnew\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
    )
    for match in new_call_re.finditer(masked):
        open_index = masked.find("(", match.end() - 1)
        close_index = _matching_paren(masked, open_index)
        if close_index is None:
            continue
        args = source[open_index + 1:close_index]
        item["calls"].append(
            {
                "kind": "constructor",
                "class": match.group(1),
                "symbol": "constructor",
                "positional_count": len(_split_top_level_csv(args)),
                "line": _line_number(source, match.start()),
            }
        )

    method_call_re = re.compile(
        r"\b([A-Za-z_$][A-Za-z0-9_$]*)\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
    )
    for match in method_call_re.finditer(masked):
        open_index = masked.find("(", match.end() - 1)
        close_index = _matching_paren(masked, open_index)
        if close_index is None:
            continue
        args = source[open_index + 1:close_index]
        item["calls"].append(
            {
                "kind": "instance_method",
                "base": match.group(1),
                "class": item["instance_types"].get(match.group(1)),
                "symbol": match.group(2),
                "positional_count": len(_split_top_level_csv(args)),
                "line": _line_number(source, match.start()),
            }
        )

    # Deduplicate exports while retaining useful metadata.
    seen_exports = set()
    exports = []
    for export in item["exports"]:
        key = export.get("symbol")
        if not key or key in seen_exports:
            continue
        seen_exports.add(key)
        exports.append(export)
    item["exports"] = exports

    return item


def _resolve_local_module(filename, specifier, available):
    for candidate in _local_candidates(filename, specifier):
        if candidate in available:
            return candidate
    return None


def _arity_issue(call, signature, label):
    if not signature:
        return None

    positional = int(call.get("positional_count") or 0)
    required = int(signature.get("required_positional") or 0)
    maximum = signature.get("max_positional")

    if positional < required:
        return (
            f"{label} is called with {positional} argument(s) but requires at least {required}."
        )
    if maximum is not None and positional > int(maximum):
        return (
            f"{label} is called with {positional} argument(s) but accepts at most {maximum}."
        )
    return None



def _invalid_node_test_context_assertions(filename, source):
    """
    node:test passes a TestContext object to the test callback.  It is not an
    assertion library and does not expose helpers such as t.equal()/t.true().
    Detect this deterministic verifier defect so the planner is allowed to
    repair test mechanics without changing the test specification.
    """
    text = str(source or "")
    callback_names = set()
    callback_re = re.compile(
        r"\b(?:test|it)\s*\(.*?,\s*(?:async\s*)?\(?\s*"
        r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\)?\s*=>",
        re.S,
    )
    for match in callback_re.finditer(text):
        callback_names.add(match.group(1))

    issues = []
    invalid_methods = {
        "equal",
        "strictEqual",
        "deepEqual",
        "deepStrictEqual",
        "true",
        "false",
        "ok",
    }
    for callback in callback_names:
        pattern = re.compile(
            r"\b" + re.escape(callback)
            + r"\.([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
        )
        for match in pattern.finditer(text):
            method = match.group(1)
            if method not in invalid_methods:
                continue
            issues.append(
                {
                    "type": "invalid_node_test_context_assertion",
                    "file": filename,
                    "line": _line_number(text, match.start()),
                    "message": (
                        f"{filename} calls {callback}.{method}(), but node:test "
                        "TestContext is not an assertion library. Use a built-in "
                        "assert module while preserving the same test behavior."
                    ),
                    "severity": "high",
                }
            )
    return issues

def _uncaptured_expected_throw_assertions(filename, source):
    """
    Detect a mechanical verifier defect where a test invokes the exact throwing
    expression immediately before assert.throws() tries to capture it.  The
    unguarded pre-call aborts the test before the assertion can run.
    """
    text = str(source or "")
    lines = text.splitlines()
    issues = []

    def normalize(value):
        value = str(value or "").strip()
        value = re.sub(r"^await\s+", "", value)
        value = value.rstrip(";").strip()
        return re.sub(r"\s+", "", value)

    for index, line in enumerate(lines):
        match = re.match(r"^\s*await\s+(.+?)\s*;\s*$", line)
        if not match:
            continue
        expression = normalize(match.group(1))
        window = "\n".join(lines[index + 1 : min(len(lines), index + 8)])
        throws_match = re.search(
            r"assert\.throws\s*\(\s*\(\s*\)\s*=>\s*([^,\n]+?)\s*,",
            window,
            re.S,
        )
        if not throws_match:
            continue
        if normalize(throws_match.group(1)) != expression:
            continue
        issues.append(
            {
                "type": "uncaptured_expected_throw",
                "file": filename,
                "line": index + 1,
                "message": (
                    f"{filename} invokes {match.group(1).strip()} before assert.throws() "
                    "tries to capture the same expected exception. Remove only the unguarded "
                    "pre-call so the verifier can observe the intended error."
                ),
                "severity": "high",
            }
        )
    return issues


def build_node_project_contract(run):
    files = list_workspace_files(
        run["user_id"],
        run["id"],
    )
    source_items = [
        item
        for item in files
        if str(item.get("filename") or "").lower().endswith(_JS_SOURCE_SUFFIXES)
    ]

    analyzed = []
    sources = {}

    for item in source_items:
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue
        try:
            source = read_workspace_file(
                run["user_id"],
                run["id"],
                filename,
                max_chars=256000,
            )
        except Exception:
            source = ""

        sources[filename] = source
        analyzed.append(
            _analyze_js_file(
                filename,
                source,
            )
        )

    package = {}
    package_source = ""
    names = {
        str(item.get("filename") or "")
        for item in files
    }
    if "package.json" in names:
        try:
            package_source = read_workspace_file(
                run["user_id"],
                run["id"],
                "package.json",
                max_chars=256000,
            )
            package = _safe_json(package_source, {})
            if not isinstance(package, dict):
                package = {}
        except Exception:
            package = {}

    available = {
        item["filename"]
        for item in analyzed
    }
    by_file = {
        item["filename"]: item
        for item in analyzed
    }
    exported = {
        filename: {
            item.get("symbol")
            for item in info.get("exports") or []
            if item.get("symbol")
        }
        for filename, info in by_file.items()
    }

    issues = []

    # Deterministic test-harness integrity.  A broken verifier must be repaired
    # as a verifier defect, not "solved" by weakening/removing test coverage.
    for info in analyzed:
        if info.get("tests") or info["filename"] in [
            item["filename"] for item in analyzed
            if item["filename"].lower().startswith("test")
        ]:
            issues.extend(
                _invalid_node_test_context_assertions(
                    info["filename"],
                    sources.get(info["filename"], ""),
                )
            )
            issues.extend(
                _uncaptured_expected_throw_assertions(
                    info["filename"],
                    sources.get(info["filename"], ""),
                )
            )

    # Validate local import contracts.
    for info in analyzed:
        for imported in info["imports"]:
            specifier = imported.get("specifier")
            if not str(specifier or "").startswith("."):
                continue

            target = _resolve_local_module(
                info["filename"],
                specifier,
                available,
            )
            if not target:
                issues.append(
                    {
                        "type": "missing_local_module",
                        "file": info["filename"],
                        "line": imported.get("line"),
                        "message": (
                            f"{info['filename']} imports {specifier}, but no matching workspace module exists."
                        ),
                        "severity": "high",
                    }
                )
                continue

            target_exports = exported.get(target) or set()
            for symbol_info in imported.get("symbols") or []:
                symbol = symbol_info.get("symbol")
                if symbol in (None, "*"):
                    continue
                if symbol == "default":
                    if "default" not in target_exports and len(target_exports) != 1:
                        issues.append(
                            {
                                "type": "missing_export",
                                "file": info["filename"],
                                "line": imported.get("line"),
                                "module": target,
                                "symbol": symbol,
                                "message": (
                                    f"{info['filename']} imports the default export from {target}, "
                                    "but the target does not expose a clear default export."
                                ),
                                "severity": "high",
                            }
                        )
                elif symbol not in target_exports:
                    issues.append(
                        {
                            "type": "missing_export",
                            "file": info["filename"],
                            "line": imported.get("line"),
                            "module": target,
                            "symbol": symbol,
                            "message": (
                                f"{info['filename']} imports {symbol} from {target}, "
                                "but that symbol is not currently exported there."
                            ),
                            "severity": "high",
                        }
                    )

    # Connect common instance calls to local class method signatures.
    class_index = {}
    for info in analyzed:
        for class_name, class_info in info["classes"].items():
            class_index.setdefault(class_name, []).append(
                (info["filename"], class_info)
            )

    for info in analyzed:
        for call in info["calls"]:
            class_name = call.get("class")
            if not class_name:
                continue
            choices = class_index.get(class_name) or []
            if len(choices) != 1:
                continue
            target_file, class_info = choices[0]
            signature = None
            label = None

            if call["kind"] == "constructor":
                signature = class_info.get("constructor")
                label = f"{target_file}:{class_name}"
            elif call["kind"] == "instance_method":
                signature = (class_info.get("methods") or {}).get(call.get("symbol"))
                label = f"{target_file}:{class_name}.{call.get('symbol')}"
                if signature is None:
                    issues.append(
                        {
                            "type": "missing_method",
                            "file": info["filename"],
                            "line": call.get("line"),
                            "module": target_file,
                            "symbol": call.get("symbol"),
                            "message": (
                                f"{info['filename']} calls {class_name}.{call.get('symbol')}(), "
                                f"but {target_file} does not define that method."
                            ),
                            "severity": "high",
                        }
                    )
                    continue

            message = _arity_issue(
                call,
                signature,
                label,
            )
            if message:
                issues.append(
                    {
                        "type": "signature_mismatch",
                        "file": info["filename"],
                        "line": call.get("line"),
                        "module": target_file,
                        "message": message,
                        "severity": "medium",
                    }
                )

    # package.json contract.
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    dependencies = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = package.get(key)
        if isinstance(value, dict):
            dependencies.update(
                {
                    str(name): str(version)
                    for name, version in value.items()
                }
            )

    test_files = [
        info["filename"]
        for info in analyzed
        if (
            info["filename"].lower().startswith("test")
            or ".test." in info["filename"].lower()
            or ".spec." in info["filename"].lower()
            or info.get("tests")
        )
    ]

    deduped = []
    seen = set()
    for issue in issues:
        key = (
            issue.get("type"),
            issue.get("file"),
            issue.get("line"),
            issue.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)

    return {
        "kind": "node",
        "file_count": len(analyzed),
        "files": analyzed,
        "issues": deduped[:80],
        "source_files": list(sources.keys()),
        "test_files": test_files,
        "package": {
            "name": package.get("name"),
            "type": package.get("type"),
            "scripts": scripts,
            "dependencies": dependencies,
        },
    }


def _normalize_failure_text(text):
    value = str(text or "")
    value = value.replace("/runtime/", "")
    value = value.replace("/workspace/", "")
    value = re.sub(r"\bduration_ms:\s*[0-9.]+", "duration_ms:#", value, flags=re.I)
    value = re.sub(r"\b\d+(?:\.\d+)?\s*ms\b", "# ms", value, flags=re.I)
    value = re.sub(r":\d+:\d+\b", ":#:#", value)
    value = re.sub(r"\bline\s+\d+\b", "line #", value, flags=re.I)
    value = re.sub(r"\b0x[0-9a-fA-F]+\b", "0x#", value)
    value = "\n".join(
        line.rstrip()
        for line in value.splitlines()
        if line.strip()
    )
    return value[-5000:]


def _failure_signature(execution):
    if not execution:
        return {
            "fingerprint": None,
            "type": None,
            "message": None,
            "filename": None,
            "status": None,
            "exit_code": None,
            "failing_tests": [],
            "location": None,
            "combined_output": "",
        }

    stdout = str(execution.get("stdout") or "")
    stderr = str(execution.get("stderr") or "")
    combined = "\n".join(
        part
        for part in (stdout, stderr)
        if part.strip()
    )

    failing_tests = [
        match.group(1).strip()
        for match in re.finditer(
            r"(?m)^\s*not ok\s+\d+\s*-\s*(.+?)\s*$",
            combined,
        )
    ][:10]

    location = None
    location_patterns = (
        r"location:\s*['\"]([^'\"]+)['\"]",
        r"(?:file://)?/runtime/([^\s():]+\.(?:js|mjs|cjs|jsx|ts|tsx)):(\d+):(\d+)",
        r"\bat\s+([^\s():]+\.(?:js|mjs|cjs|jsx|ts|tsx)):(\d+):(\d+)",
    )
    for pattern in location_patterns:
        match = re.search(pattern, combined)
        if match:
            location = ":".join(str(group) for group in match.groups() if group is not None)
            location = location.replace("/runtime/", "")
            break

    failure_type = None
    for candidate in (
        "AssertionError",
        "TypeError",
        "ReferenceError",
        "SyntaxError",
        "RangeError",
        "URIError",
        "AggregateError",
    ):
        if candidate in combined:
            failure_type = candidate
            break
    if not failure_type:
        failure_type = "node_test_failure" if failing_tests else "node_execution_failure"

    normalized = _normalize_failure_text(combined)

    # Prefer stable contract-bearing evidence over the full TAP transcript.
    assertion_lines = []
    for line in normalized.splitlines():
        lowered = line.lower()
        if (
            "expected" in lowered
            or "actual" in lowered
            or "assertion" in lowered
            or "error:" in lowered
            or "operator:" in lowered
        ):
            assertion_lines.append(line.strip())

    message_parts = []
    if failing_tests:
        message_parts.append("failing_tests=" + " | ".join(failing_tests))
    if location:
        message_parts.append("location=" + location)
    if assertion_lines:
        message_parts.append("assertion=" + " | ".join(assertion_lines[-8:]))
    elif normalized:
        message_parts.append(normalized[-1800:])

    message = " ; ".join(message_parts)[:3000]

    raw = "|".join(
        [
            str(execution.get("execution_action") or ""),
            str(execution.get("filename") or ""),
            str(execution.get("command") or ""),
            failure_type,
            " | ".join(failing_tests),
            str(location or ""),
            message,
        ]
    )
    fingerprint = hashlib.sha1(
        raw.encode("utf-8", errors="ignore")
    ).hexdigest()[:16]

    return {
        "fingerprint": fingerprint,
        "type": failure_type,
        "message": message,
        "filename": execution.get("filename"),
        "status": execution.get("status"),
        "exit_code": execution.get("exit_code"),
        "failing_tests": failing_tests,
        "location": location,
        "combined_output": normalized[-5000:],
    }


def _execution_analysis(run):
    rows = [
        item
        for item in list_agent_sandbox_executions(
            run["user_id"],
            run["id"],
            limit=100,
        )
        if str(item.get("runtime") or "python").lower() == "node"
    ]

    if not rows:
        return {
            "latest": None,
            "failure": _failure_signature(None),
            "repeated_failure_count": 0,
            "progress_state": "untested",
            "executions": [],
        }

    latest = rows[-1]
    if (
        str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
    ):
        return {
            "latest": latest,
            "failure": _failure_signature(latest),
            "repeated_failure_count": 0,
            "progress_state": "verified_execution",
            "executions": rows,
        }

    latest_failure = _failure_signature(latest)
    repeated = 0

    for item in reversed(rows):
        if (
            str(item.get("status") or "") == "success"
            and int(item.get("exit_code") or 0) == 0
        ):
            break
        signature = _failure_signature(item)
        if signature["fingerprint"] == latest_failure["fingerprint"]:
            repeated += 1
        else:
            break

    progress_state = "stalled" if repeated >= 2 else "new_failure"

    if len(rows) >= 2:
        previous = rows[-2]
        if not (
            str(previous.get("status") or "") == "success"
            and int(previous.get("exit_code") or 0) == 0
        ):
            previous_failure = _failure_signature(previous)
            if previous_failure["fingerprint"] != latest_failure["fingerprint"]:
                progress_state = "failure_changed_progress"

    return {
        "latest": latest,
        "failure": latest_failure,
        "repeated_failure_count": repeated,
        "progress_state": progress_state,
        "executions": rows,
    }


def _repair_churn(run):
    steps = list_agent_steps(
        run["user_id"],
        run["id"],
    )[-18:]
    names = []
    pattern = re.compile(
        r"(?:Created|Updated) workspace file:\s*([^\s(]+)"
    )
    for step in steps:
        if str(step.get("action") or "") != "write_file":
            continue
        match = pattern.search(str(step.get("output") or ""))
        if match:
            names.append(match.group(1))

    if not names:
        return 0

    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return max(counts.values())


def analyze_node_project_state(run):
    initialize_agent_project_planner_storage()

    contract = build_node_project_contract(run)
    execution = _execution_analysis(run)
    churn = _repair_churn(run)
    acceptance = evaluate_acceptance_contract(
        run,
        contract,
        sandbox_verified=None,
    )

    latest = execution.get("latest")
    execution_verified = bool(
        latest
        and str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
    )
    fingerprint_parts = []
    if not execution_verified and execution["failure"].get("fingerprint"):
        fingerprint_parts.append(
            "execution:" + str(execution["failure"]["fingerprint"])
        )
    if not acceptance.get("satisfied") and acceptance.get("fingerprint"):
        fingerprint_parts.append(
            "acceptance:" + str(acceptance["fingerprint"])
        )
    planning_fingerprint = (
        hashlib.sha1(
            "|".join(fingerprint_parts).encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        if fingerprint_parts
        else None
    )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT escalation_count
        FROM agent_project_states
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
        ),
    )
    existing = cursor.fetchone()
    escalation_count = int(existing[0] or 0) if existing else 0

    cursor.execute(
        """
        INSERT INTO agent_project_states (
            run_id,
            user_id,
            project_kind,
            contract_json,
            latest_failure_fingerprint,
            repeated_failure_count,
            progress_state,
            contract_issue_count,
            repair_churn_count,
            escalation_count,
            last_planner_tier,
            last_planner_model,
            updated_at
        )
        VALUES (?, ?, 'node', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(run_id)
        DO UPDATE SET
            project_kind = 'node',
            contract_json = excluded.contract_json,
            latest_failure_fingerprint = excluded.latest_failure_fingerprint,
            repeated_failure_count = excluded.repeated_failure_count,
            progress_state = excluded.progress_state,
            contract_issue_count = excluded.contract_issue_count,
            repair_churn_count = excluded.repair_churn_count,
            updated_at = excluded.updated_at
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            json.dumps(contract, ensure_ascii=False),
            planning_fingerprint,
            int(execution["repeated_failure_count"]),
            execution["progress_state"],
            len(contract["issues"]),
            int(churn),
            escalation_count,
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()

    return {
        "contract": contract,
        "execution": execution,
        "acceptance": acceptance,
        "planning_fingerprint": planning_fingerprint,
        "repair_churn_count": churn,
        "escalation_count": escalation_count,
    }


def _debug_plan_count_for_failure(user_id, run_id, fingerprint):
    if not fingerprint:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM agent_debug_plans
        WHERE
            run_id = ?
            AND user_id = ?
            AND failure_fingerprint = ?
        """,
        (
            str(run_id),
            int(user_id),
            str(fingerprint),
        ),
    )
    count = int(cursor.fetchone()[0] or 0)
    conn.close()
    return count


def node_active_plan_matches_current_failure(
    user_id,
    run_id,
    current_failure_fingerprint,
):
    plan = get_active_debug_plan(
        user_id,
        run_id,
    )
    if not plan:
        return True

    planned = str(plan.get("failure_fingerprint") or "").strip()
    current = str(current_failure_fingerprint or "").strip()
    if not planned or not current:
        return True
    return planned == current


def structured_node_planner_exhausted_for_current_failure(run, analysis=None):
    analysis = analysis or analyze_node_project_state(run)
    fingerprint = analysis.get("planning_fingerprint")
    if not fingerprint:
        return False
    if AGENT_EXPERT_MODEL:
        return False

    plan_count = _debug_plan_count_for_failure(
        run["user_id"],
        run["id"],
        fingerprint,
    )
    repeated = int(
        analysis["execution"].get("repeated_failure_count")
        or 0
    )
    acceptance_incomplete = not (analysis.get("acceptance") or {}).get("satisfied", True)
    return plan_count >= 2 and (repeated >= 2 or acceptance_incomplete)


def should_create_node_debug_plan(run, analysis=None):
    analysis = analysis or analyze_node_project_state(run)
    latest = analysis["execution"]["latest"]

    if not latest:
        return False
    execution_verified = bool(
        str(latest.get("status") or "") == "success"
        and int(latest.get("exit_code") or 0) == 0
    )
    if execution_verified and (analysis.get("acceptance") or {}).get("satisfied", True):
        return False

    active = get_active_debug_plan(
        run["user_id"],
        run["id"],
    )
    if active:
        repairs = list(
            active["plan"].get("repair_sequence")
            or []
        )
        if int(active.get("next_repair_index") or 0) < len(repairs):
            return False

    contract = analysis["contract"]
    execution = analysis["execution"]
    repeated = int(execution.get("repeated_failure_count") or 0)
    churn = int(analysis.get("repair_churn_count") or 0)
    failure_type = str(execution["failure"].get("type") or "")

    if not (analysis.get("acceptance") or {}).get("satisfied", True):
        return True
    if contract["issues"]:
        return True
    if repeated >= 2 or churn >= 2:
        return True

    # For a multi-file Node project, even the first real assertion/runtime
    # failure benefits from an explicit cross-file contract before speculative
    # rewrites begin.
    if (
        contract["file_count"] >= 2
        and failure_type in {
            "AssertionError",
            "TypeError",
            "ReferenceError",
            "SyntaxError",
            "node_test_failure",
            "node_execution_failure",
        }
    ):
        return True

    return False


def _choose_planner_model(run, analysis):
    mode = str(run.get("model_mode") or "auto").strip().lower()

    if mode != "auto":
        selected_mode, selected_model = base_runner._select_agent_model(run)
        return {
            "tier": f"manual_{selected_mode}",
            "model": selected_model,
            "escalated": False,
        }

    repeated = int(
        analysis["execution"].get("repeated_failure_count")
        or 0
    )
    issues = len(analysis["contract"]["issues"])
    acceptance_issues = int((analysis.get("acceptance") or {}).get("issue_count") or 0)
    churn = int(analysis.get("repair_churn_count") or 0)
    fingerprint = analysis.get("planning_fingerprint")
    plan_count = _debug_plan_count_for_failure(
        run["user_id"],
        run["id"],
        fingerprint,
    )

    if (
        AUTO_ESCALATION_ENABLED
        and AGENT_EXPERT_MODEL
        and (
            repeated >= 4
            or plan_count >= 2
            or issues + acceptance_issues >= 6
        )
    ):
        return {
            "tier": "expert",
            "model": AGENT_EXPERT_MODEL,
            "escalated": True,
        }

    # v2.4: old lifetime repair churn must not permanently force every new
    # project decision onto the slow reasoning model. Small/local projects stay
    # worker-first; escalation is reserved for a repeated plan against the SAME
    # current fingerprint or materially larger project complexity.
    file_count = int(analysis.get("contract", {}).get("file_count") or 0)
    complexity = issues + acceptance_issues
    if (
        AUTO_ESCALATION_ENABLED
        and (
            plan_count >= 1
            or file_count > 10
            or complexity >= 10
        )
    ):
        return {
            "tier": "reasoning",
            "model": AGENT_REASONING_MODEL,
            "escalated": True,
        }

    return {
        "tier": "worker",
        "model": AGENT_WORKER_MODEL,
        "escalated": False,
    }


def _contract_summary(analysis):
    contract = analysis["contract"]
    execution = analysis["execution"]

    lines = [
        f"Project kind: Node.js/JavaScript",
        f"Source files: {contract['file_count']}",
        f"Test files: {', '.join(contract['test_files']) if contract['test_files'] else 'none detected'}",
        f"Progress state: {execution['progress_state']}",
        f"Repeated latest failure: {execution['repeated_failure_count']}",
        f"Recent repair churn: {analysis['repair_churn_count']}",
    ]

    lines.append(acceptance_summary(analysis.get("acceptance")))

    package = contract.get("package") or {}
    scripts = package.get("scripts") or {}
    dependencies = package.get("dependencies") or {}
    lines.append(
        "npm scripts: "
        + (
            ", ".join(
                f"{name}={value}"
                for name, value in scripts.items()
            )
            if scripts
            else "none"
        )
    )
    lines.append(
        "npm dependencies: "
        + (
            ", ".join(
                f"{name}@{value}"
                for name, value in dependencies.items()
            )
            if dependencies
            else "none"
        )
    )

    failure = execution["failure"]
    if failure.get("fingerprint"):
        lines.append(
            "Latest failure: "
            + str(failure.get("type") or "failure")
            + " | "
            + str(failure.get("message") or "")
        )

    if contract["issues"]:
        lines.append("Static contract issues:")
        for issue in contract["issues"][:25]:
            location = issue.get("file") or "workspace"
            if issue.get("line"):
                location += f":{issue['line']}"
            lines.append(
                f"- [{issue.get('type')}] {location}: {issue.get('message')}"
            )
    else:
        lines.append("Static contract issues: none detected.")

    lines.append("Module API/test map:")
    for item in contract["files"][:25]:
        defs = []
        defs.extend(
            name + info.get("display", "")
            for name, info in item["functions"].items()
        )
        for class_name, class_info in item["classes"].items():
            methods = ", ".join(
                name + info.get("display", "")
                for name, info in class_info["methods"].items()
                if name != "constructor"
            )
            defs.append(
                f"class {class_name}{class_info['constructor'].get('display', '()')}"
                + (f" methods[{methods}]" if methods else "")
            )

        exports = ", ".join(
            str(export.get("symbol"))
            for export in item["exports"]
            if export.get("symbol")
        )
        tests = ", ".join(
            test.get("name")
            for test in item["tests"][:12]
            if test.get("name")
        )

        line = (
            f"- {item['filename']}: API="
            + ("; ".join(defs) if defs else "none detected")
        )
        if exports:
            line += f" | exports={exports}"
        if tests:
            line += f" | tests={tests}"
        lines.append(line)

        for assertion in item["assertions"][:12]:
            lines.append(
                f"    assertion L{assertion['line']}: {assertion['text']}"
            )

    return "\n".join(lines)[:18000]


def _workspace_source_bundle(run, analysis):
    priority = []

    def add(name):
        if name and name not in priority:
            priority.append(name)

    failure = analysis["execution"]["failure"]
    location = str(failure.get("location") or "")
    if location:
        add(location.split(":", 1)[0])

    add(str(failure.get("filename") or ""))

    for item in analysis["contract"]["test_files"]:
        add(item)

    for issue in analysis["contract"]["issues"]:
        add(str(issue.get("file") or ""))
        add(str(issue.get("module") or ""))

    for item in analysis["contract"]["files"]:
        add(item["filename"])

    if analysis["contract"].get("package"):
        add("package.json")

    available = {
        item["filename"]
        for item in analysis["contract"]["files"]
    }
    available.add("package.json")

    blocks = []
    used = 0

    for filename in priority:
        if filename not in available or used >= PROJECT_SOURCE_BUDGET:
            continue
        try:
            content = read_workspace_file(
                run["user_id"],
                run["id"],
                filename,
                max_chars=PROJECT_FILE_BUDGET,
            )
        except Exception:
            continue

        remaining = PROJECT_SOURCE_BUDGET - used
        block = (
            f"--- {filename} ---\n"
            + str(content or "")[:remaining]
        )
        blocks.append(block)
        used += len(block)

    return "\n\n".join(blocks)


def _execution_history_text(analysis):
    blocks = []
    for item in (analysis["execution"].get("executions") or [])[-6:]:
        failure = _failure_signature(item)
        blocks.append(
            (
                f"{item.get('execution_action')} "
                f"{item.get('filename') or item.get('command')} | "
                f"{item.get('status')} | exit={item.get('exit_code')} | "
                f"fp={failure.get('fingerprint')}\n"
                + failure.get("combined_output", "")[-2600:]
            )
        )
    return "\n\n".join(blocks)[-12000:]


def _environment_profile_text(run):
    profile = sandbox_runtime_profile(
        run["user_id"],
        run["id"],
    )
    return (
        "Sandbox capability profile:\n"
        f"- durable source mount: {profile['source_mount']} (read-only)\n"
        f"- execution working directory: {profile['runtime_workdir']} (writable)\n"
        f"- temp directory: {profile['tmp_dir']} (writable)\n"
        f"- execution network: {'enabled' if profile.get('execution_network') else 'disabled'}\n"
        f"- dependency setup network: {'enabled' if profile.get('setup_network') else 'disabled'}\n"
        f"- controlled dependency setup available: {'yes' if profile.get('dependency_installation') else 'no'}\n"
        f"- dependency note: {profile.get('dependency_note')}\n"
        "Normal npm/test execution remains network-off. Do not rewrite requested "
        "dependencies away simply because normal execution cannot access the network."
    )


def _run_structured_model(
    run,
    system_prompt,
    user_prompt,
    models,
    label,
    validator=None,
):
    """
    Structured-output reliability belongs inside one logical Agent action.

    A malformed JSON response or a semantically invalid repair target should
    not consume another lifetime Agent step.  Re-prompt/fallback happens here,
    while the outer step ledger still records one project_plan/project_repair.
    """
    last_error = None
    last_raw = ""

    for attempt, model in enumerate(list(models)[:3], start=1):
        retry_note = ""
        if last_error:
            retry_note = (
                "\n\nINTERNAL STRUCTURED-OUTPUT RETRY:\n"
                f"The previous response was rejected: {last_error}\n"
                "Return one strict JSON object matching the requested schema. "
                "Do not repeat the rejected mistake."
            )
            if last_raw:
                retry_note += (
                    "\nPrevious response excerpt:\n"
                    + last_raw[-1800:]
                )

        try:
            raw, actual_model = base_runner._run_model(
                run,
                system_prompt,
                user_prompt + retry_note,
                response_format="json",
                model_override=model,
            )
        except Exception as error:
            last_error = str(error)
            last_raw = ""
            continue

        last_raw = str(raw or "")
        try:
            data = base_runner._safe_json_object(
                raw,
                label,
            )
        except Exception as error:
            last_error = str(error)
            continue

        if validator:
            validation_error = validator(data)
            if validation_error:
                last_error = str(validation_error)
                continue

        return data, actual_model

    raise AgentNodeProjectPlannerError(
        f"{label} could not produce a valid structured result inside its internal retry budget"
        + (f": {last_error}" if last_error else ".")
    )


def _test_repair_allowed(filename, analysis):
    if not is_test_file(filename, analysis.get("contract")):
        return False

    test_issue_types = {
        "insufficient_test_count",
        "missing_required_test_behavior",
    }
    if any(
        issue.get("type") in test_issue_types
        for issue in (analysis.get("acceptance") or {}).get("issues") or []
    ):
        return True

    if any(
        issue.get("file") == filename
        and issue.get("type") in {
            "invalid_node_test_context_assertion",
            "uncaptured_expected_throw",
            "missing_local_module",
            "missing_export",
            "signature_mismatch",
        }
        for issue in analysis.get("contract", {}).get("issues") or []
    ):
        return True

    failure = analysis.get("execution", {}).get("failure") or {}
    location = str(failure.get("location") or "")
    return bool(
        location.startswith(filename + ":")
        and str(failure.get("type") or "") in {
            "SyntaxError",
            "TypeError",
            "ReferenceError",
        }
    )


def _candidate_contract_error(run, analysis, target, previous, content):
    if is_test_file(target, analysis.get("contract")):
        return validate_test_candidate(
            run,
            target,
            previous,
            content,
            analysis.get("contract") or {},
        )

    if target == "package.json":
        try:
            old = json.loads(previous or "{}") if str(previous or "").strip() else {}
            new = json.loads(content or "{}")
        except Exception as error:
            return f"package.json repair is not valid JSON: {error}"
        if not isinstance(new, dict):
            return "package.json repair must be a JSON object."

        acceptance = get_or_create_acceptance_contract(run)
        new_scripts = new.get("scripts") if isinstance(new.get("scripts"), dict) else {}
        new_dependencies = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            value = new.get(key)
            if isinstance(value, dict):
                new_dependencies.update(value)

        missing = [
            dep
            for dep in acceptance.get("required_dependencies") or []
            if dep not in new_dependencies
        ]
        if missing:
            return "Acceptance guard rejected package.json because required dependencies would be missing: " + ", ".join(missing)

        missing_scripts = [
            script
            for script in acceptance.get("required_scripts") or []
            if script not in new_scripts
        ]
        if missing_scripts:
            return "Acceptance guard rejected package.json because required scripts would be missing: " + ", ".join(missing_scripts)
        return None

    if not str(target).lower().endswith(_JS_SOURCE_SUFFIXES):
        return None

    old_info = None
    for item in analysis.get("contract", {}).get("files") or []:
        if item.get("filename") == target:
            old_info = item
            break
    if not old_info:
        # Creating an explicitly required missing file has no old API to protect.
        return None

    new_info = _analyze_js_file(target, content)

    old_exports = {
        item.get("symbol")
        for item in old_info.get("exports") or []
        if item.get("symbol")
    }
    new_exports = {
        item.get("symbol")
        for item in new_info.get("exports") or []
        if item.get("symbol")
    }
    missing_exports = sorted(old_exports - new_exports)
    if missing_exports:
        return "Contract-regression guard rejected the repair because it removed export(s): " + ", ".join(missing_exports)

    old_functions = set((old_info.get("functions") or {}).keys())
    new_functions = set((new_info.get("functions") or {}).keys())
    missing_functions = sorted(old_functions - new_functions)
    if missing_functions:
        return "Contract-regression guard rejected the repair because it removed function(s): " + ", ".join(missing_functions)

    old_classes = old_info.get("classes") or {}
    new_classes = new_info.get("classes") or {}
    missing_classes = sorted(set(old_classes) - set(new_classes))
    if missing_classes:
        return "Contract-regression guard rejected the repair because it removed class(es): " + ", ".join(missing_classes)

    for class_name, class_info in old_classes.items():
        old_methods = set((class_info.get("methods") or {}).keys())
        new_methods = set(((new_classes.get(class_name) or {}).get("methods") or {}).keys())
        missing_methods = sorted(old_methods - new_methods)
        if missing_methods:
            return (
                f"Contract-regression guard rejected the repair because {class_name} "
                "lost method(s): " + ", ".join(missing_methods)
            )

    return None

def _preferred_verification(analysis):
    scripts = (
        analysis["contract"]
        .get("package", {})
        .get("scripts", {})
    )
    for name in (
        "test",
        "check",
        "lint",
        "typecheck",
        "build",
    ):
        if name in scripts:
            return {
                "kind": "npm_script",
                "script": name,
            }

    tests = analysis["contract"].get("test_files") or []
    if tests:
        return {
            "kind": "node_file",
            "filename": tests[0],
        }

    files = analysis["contract"].get("source_files") or []
    return (
        {
            "kind": "node_file",
            "filename": files[0],
        }
        if files
        else {}
    )


def create_node_debug_plan(run, analysis=None):
    initialize_agent_project_planner_storage()
    analysis = analysis or analyze_node_project_state(run)
    choice = _choose_planner_model(
        run,
        analysis,
    )

    system_prompt = (
        "You are the senior Node.js/JavaScript project-contract planner for a persistent "
        "local engineering Agent. Do NOT rewrite code in this response. Produce a small, "
        "explicit repair plan grounded in the deterministic project contract, current "
        "source, test assertions, package.json and actual sandbox output. Treat imports, "
        "exports, class/function signatures, callers, tests and npm scripts as one project "
        "contract. Preserve the user's requested architecture and dependencies. Tests are "
        "specifications when they reflect the user goal; never edit tests merely to force "
        "green results. Prefer fixing implementation code. A changed failure fingerprint "
        "is progress; an unchanged fingerprint means the current hypothesis failed.\n\n"
        "Return ONLY JSON with keys:\n"
        "summary (string), root_cause (string), confidence (0-1), "
        "contract_decisions (array of short strings), "
        "repair_sequence (array of objects with file, objective, reason), "
        "verification (object with kind plus script or filename), "
        "stop_condition (string).\n\n"
        "Repair_sequence should normally contain 1-4 workspace files. Targets may be "
        "existing files or explicitly REQUIRED-MISSING files from the acceptance contract. "
        "Do not invent unrelated files. A test file is protected specification: include it "
        "only when deterministic evidence shows the test harness itself is defective or "
        "the acceptance contract proves required test coverage is missing. Keep the plan coherent across "
        "the whole project rather than repeatedly guessing changes in one file."
    )

    user_prompt = (
        "USER GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nUSER REVISION/INPUT HISTORY:\n"
        + base_runner._inputs_text(run)
        + "\n\nDETERMINISTIC NODE PROJECT CONTRACT:\n"
        + _contract_summary(analysis)
        + "\n\nCURRENT SOURCE SNAPSHOT:\n"
        + (_workspace_source_bundle(run, analysis) or "No source available.")
        + "\n\nRECENT SANDBOX EVIDENCE:\n"
        + (_execution_history_text(analysis) or "No execution evidence.")
        + "\n\nSANDBOX CAPABILITY PROFILE:\n"
        + _environment_profile_text(run)
    )

    existing_files = {
        item["filename"]
        for item in analysis["contract"]["files"]
    }
    existing_files.add("package.json")
    acceptance_contract = get_or_create_acceptance_contract(run)
    required_missing_files = {
        filename
        for filename in acceptance_contract.get("required_files") or []
        if filename not in existing_files
    }
    allowed_targets = existing_files | required_missing_files

    def _plan_validator(data):
        sequence = data.get("repair_sequence")
        if not isinstance(sequence, list):
            return "repair_sequence must be an array."

        useful = 0
        for item in sequence[:6]:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("file") or "").strip()
            if filename not in allowed_targets:
                continue
            if is_test_file(filename, analysis["contract"]) and not _test_repair_allowed(
                filename,
                analysis,
            ):
                continue
            useful += 1

        if useful <= 0:
            return (
                "The plan contains no allowed repair target. Use an existing workspace "
                "file or an explicitly required missing deliverable, and do not rewrite "
                "protected tests without deterministic justification."
            )
        return None

    data, actual_model = _run_structured_model(
        run,
        system_prompt,
        user_prompt,
        [choice["model"], AGENT_WORKER_MODEL],
        "Node project debug planner",
        validator=_plan_validator,
    )

    repairs = []
    for item in list(data.get("repair_sequence") or [])[:6]:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("file") or "").strip()
        if filename not in allowed_targets:
            continue
        if is_test_file(filename, analysis["contract"]) and not _test_repair_allowed(
            filename,
            analysis,
        ):
            continue
        repairs.append(
            {
                "file": filename,
                "objective": str(item.get("objective") or "").strip()[:1400],
                "reason": str(item.get("reason") or "").strip()[:1400],
            }
        )

    verification = data.get("verification")
    if not isinstance(verification, dict):
        verification = {}

    preferred = _preferred_verification(analysis)
    kind = str(verification.get("kind") or "").strip().lower()
    if kind == "npm_script":
        scripts = (
            analysis["contract"]
            .get("package", {})
            .get("scripts", {})
        )
        script = str(verification.get("script") or "").strip()
        if script not in scripts:
            verification = preferred
        else:
            verification = {
                "kind": "npm_script",
                "script": script,
            }
    elif kind == "node_file":
        filename = str(verification.get("filename") or "").strip()
        if filename not in existing_files:
            verification = preferred
        else:
            verification = {
                "kind": "node_file",
                "filename": filename,
            }
    else:
        verification = preferred

    plan = {
        "project_kind": "node",
        "summary": str(data.get("summary") or "").strip()[:2200],
        "root_cause": str(data.get("root_cause") or "").strip()[:3200],
        "confidence": _clamp_float(
            data.get("confidence"),
            0.0,
            1.0,
            0.7,
        ),
        "blocked_by_environment": False,
        "environment_note": "",
        "contract_decisions": [
            str(item).strip()[:1200]
            for item in list(data.get("contract_decisions") or [])[:12]
            if str(item).strip()
        ],
        "repair_sequence": repairs,
        "verification": verification,
        "stop_condition": str(data.get("stop_condition") or "").strip()[:1800],
        "analysis": {
            "progress_state": analysis["execution"]["progress_state"],
            "repeated_failure_count": analysis["execution"]["repeated_failure_count"],
            "contract_issue_count": len(analysis["contract"]["issues"]),
            "acceptance_issue_count": int((analysis.get("acceptance") or {}).get("issue_count") or 0),
            "repair_churn_count": analysis["repair_churn_count"],
        },
    }

    fingerprint = analysis.get("planning_fingerprint")
    trigger = _planner_trigger(analysis)
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET status = 'superseded', completed_at = ?
        WHERE run_id = ? AND user_id = ? AND status = 'active'
        """,
        (
            timestamp,
            str(run["id"]),
            int(run["user_id"]),
        ),
    )

    cursor.execute(
        """
        INSERT INTO agent_debug_plans (
            run_id,
            user_id,
            trigger,
            failure_fingerprint,
            planner_tier,
            planner_model,
            plan_json,
            next_repair_index,
            status,
            created_at,
            completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, NULL)
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            trigger,
            fingerprint,
            choice["tier"],
            actual_model,
            json.dumps(plan, ensure_ascii=False),
            timestamp,
        ),
    )
    plan_id = cursor.lastrowid

    cursor.execute(
        """
        UPDATE agent_project_states
        SET
            escalation_count = escalation_count + ?,
            last_planner_tier = ?,
            last_planner_model = ?,
            updated_at = ?
        WHERE run_id = ? AND user_id = ?
        """,
        (
            1 if choice["escalated"] else 0,
            choice["tier"],
            actual_model,
            timestamp,
            str(run["id"]),
            int(run["user_id"]),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "id": plan_id,
        "run_id": run["id"],
        "user_id": run["user_id"],
        "trigger": trigger,
        "failure_fingerprint": fingerprint,
        "planner_tier": choice["tier"],
        "planner_model": actual_model,
        "plan": plan,
        "next_repair_index": 0,
        "status": "active",
        "created_at": timestamp,
        "completed_at": None,
    }


def _planner_trigger(analysis):
    if not (analysis.get("acceptance") or {}).get("satisfied", True):
        return "node_acceptance_contract"
    if analysis["contract"]["issues"]:
        return "node_project_contract_mismatch"
    if analysis["execution"]["repeated_failure_count"] >= 2:
        return "node_repeated_failure"
    if analysis["repair_churn_count"] >= 2:
        return "node_repair_churn"
    return "node_cross_file_failure"


def format_node_debug_plan(plan):
    data = plan["plan"]
    lines = [
        "Node.js project contract/debug plan created.",
        f"Trigger: {plan['trigger']}",
        f"Planner tier: {plan['planner_tier']}",
        f"Planner model: {plan['planner_model']}",
        f"Failure fingerprint: {plan.get('failure_fingerprint') or 'none'}",
        "Root cause: "
        + str(
            data.get("root_cause")
            or data.get("summary")
            or "Not specified"
        ),
    ]

    acceptance_issue_count = int(
        (data.get("analysis") or {}).get("acceptance_issue_count")
        or 0
    )
    if acceptance_issue_count:
        lines.append(
            f"Acceptance requirements still open when planned: {acceptance_issue_count}"
        )

    repairs = list(data.get("repair_sequence") or [])
    if repairs:
        lines.append("Repair sequence:")
        for index, item in enumerate(repairs, start=1):
            lines.append(
                f"{index}. {item.get('file')}: "
                f"{item.get('objective') or item.get('reason')}"
            )
    else:
        lines.append("Repair sequence: no code rewrite recommended by planner.")

    verification = data.get("verification") or {}
    if verification:
        if verification.get("kind") == "npm_script":
            lines.append(
                "Verification: npm run "
                + str(verification.get("script"))
            )
        elif verification.get("kind") == "node_file":
            lines.append(
                "Verification: node "
                + str(verification.get("filename"))
            )

    return "\n".join(lines)[:6500]


def node_project_planner_context(run, analysis=None):
    analysis = analysis or analyze_node_project_state(run)
    active = get_active_debug_plan(
        run["user_id"],
        run["id"],
    )

    text = _contract_summary(analysis)
    if active:
        text += (
            "\n\nACTIVE NODE DEBUG PLAN:\n"
            + format_node_debug_plan(active)
            + f"\nNext repair index: {active['next_repair_index']}"
        )

    return text[:20000]


def _repair_prompt_context(run, analysis, plan, repair):
    target = repair["file"]
    try:
        current = read_workspace_file(
            run["user_id"],
            run["id"],
            target,
            max_chars=PROJECT_FILE_BUDGET,
        )
    except Exception:
        current = "<REQUIRED FILE IS CURRENTLY MISSING; CREATE IT>"

    return (
        "USER GOAL:\n"
        + str(run.get("goal") or "")
        + "\n\nUSER REVISION/INPUT HISTORY:\n"
        + base_runner._inputs_text(run)
        + "\n\nNODE PROJECT CONTRACT:\n"
        + _contract_summary(analysis)
        + "\n\nACTIVE DEBUG PLAN:\n"
        + json.dumps(
            plan["plan"],
            ensure_ascii=False,
            indent=2,
        )[:11000]
        + "\n\nTARGET REPAIR:\n"
        + json.dumps(
            repair,
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nCURRENT TARGET FILE:\n--- "
        + target
        + " ---\n"
        + str(current or "")
        + "\n\nRELATED CURRENT WORKSPACE:\n"
        + _workspace_source_bundle(run, analysis)
        + "\n\nRECENT SANDBOX EVIDENCE:\n"
        + _execution_history_text(analysis)
    )


def execute_node_project_repair(run):
    next_item = get_next_project_repair(
        run["user_id"],
        run["id"],
    )
    if not next_item:
        raise AgentNodeProjectPlannerError(
            "The active Node project plan has no remaining repair step."
        )

    plan = next_item["plan"]
    repair = next_item["repair"]
    target = str(repair.get("file") or "").strip()

    analysis = analyze_node_project_state(run)
    existing = {
        item["filename"]
        for item in analysis["contract"]["files"]
    }
    existing.add("package.json")
    acceptance_contract = get_or_create_acceptance_contract(run)
    allowed_missing = {
        filename
        for filename in acceptance_contract.get("required_files") or []
        if filename not in existing
    }
    allowed_targets = existing | allowed_missing

    if target not in allowed_targets:
        raise AgentNodeProjectPlannerError(
            f"Planned Node repair target is not an existing or required-missing file: {target}"
        )

    if is_test_file(target, analysis["contract"]) and not _test_repair_allowed(
        target,
        analysis,
    ):
        raise AgentNodeProjectPlannerError(
            f"Test-integrity guard blocked an unjustified repair of protected test file: {target}"
        )

    if target in existing:
        previous = read_workspace_file(
            run["user_id"],
            run["id"],
            target,
            max_chars=256000,
        )
    else:
        previous = ""

    system_prompt = (
        "You are the Node.js implementation specialist executing ONE approved repair "
        "from a persistent project debug plan. Rewrite exactly the TARGET FILE and no "
        "other file. Return the COMPLETE file content, not a patch. Preserve the user's "
        "architecture and requested npm dependencies. Reconcile imports/exports, APIs, "
        "callers, test expectations, acceptance requirements and package contract shown "
        "below. Existing public APIs are regression-protected during debugging. Test files "
        "are protected specification: when a test-harness repair is explicitly approved, "
        "preserve every existing test name and restore/retain all goal-required coverage; "
        "never delete tests merely to get green. Make the smallest coherent change that "
        "satisfies this repair objective.\n\n"
        "Return ONLY JSON with keys: filename, content, summary. The filename MUST exactly "
        "match the TARGET FILE."
    )

    context = _repair_prompt_context(
        run,
        analysis,
        plan,
        repair,
    )

    def _repair_validator(data):
        returned_filename = str(data.get("filename") or "").strip()
        if returned_filename != target:
            return (
                f"filename must be exactly {target}; received "
                f"{returned_filename or '<empty>'}."
            )
        content = data.get("content")
        if content is None or not isinstance(content, str):
            return "content must contain the complete target file as a string."
        return _candidate_contract_error(
            run,
            analysis,
            target,
            previous,
            content,
        )

    data, actual_model = _run_structured_model(
        run,
        system_prompt,
        context,
        [
            AGENT_WORKER_MODEL,
            plan.get("planner_model") or AGENT_WORKER_MODEL,
            AGENT_WORKER_MODEL,
        ],
        "Node project repair specialist",
        validator=_repair_validator,
    )

    content = str(data.get("content") or "")
    if str(previous) == content:
        _advance_plan(
            plan["id"],
            run["user_id"],
        )
        return (
            f"Planner-guided Node repair inspected {target} using {actual_model}, "
            "but the validated content was unchanged. Advanced to the next planned repair."
        )

    result = write_workspace_file(
        run["user_id"],
        run["id"],
        target,
        content,
    )
    _advance_plan(
        plan["id"],
        run["user_id"],
    )

    verb = "created" if target not in existing else "updated"
    return (
        f"Planner-guided repair {verb} {result['filename']} ({result['size_bytes']} bytes).\n"
        f"Repair model: {actual_model}\n"
        f"Objective: {repair.get('objective') or repair.get('reason') or 'Repair Node project contract'}\n"
        f"Summary: {str(data.get('summary') or '').strip()}\n"
        "Project/test-integrity guards validated the candidate before the workspace mutation. "
        "The deterministic execution loop will re-test the current workspace before another repair."
    )[:6500]

def _advance_plan(plan_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE agent_debug_plans
        SET next_repair_index = next_repair_index + 1
        WHERE id = ? AND user_id = ? AND status = 'active'
        """,
        (
            int(plan_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()


def _clamp_float(value, low, high, fallback):
    try:
        number = float(value)
    except Exception:
        return fallback
    return max(low, min(high, number))
