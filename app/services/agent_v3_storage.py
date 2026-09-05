"""ATLAS v3 Agent Core persistent execution state and telemetry.

The v3 core keeps orchestration state separate from the legacy agent_runs table so
existing UI/API/storage contracts remain backward compatible while the execution
engine can evolve cleanly.  All schema changes are additive.
"""

import json
import threading

from app.database import get_connection
from app.services.agents import utc_iso

CORE_VERSION = "3.14.0"
_STORAGE_READY = False
_STORAGE_LOCK = threading.Lock()


def _json(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value, default=None):
    if default is None:
        default = {}
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return default.copy() if hasattr(default, "copy") else default
    return parsed


def initialize_v3_storage():
    global _STORAGE_READY
    if _STORAGE_READY:
        return
    with _STORAGE_LOCK:
        if _STORAGE_READY:
            return
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_runs (
                run_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                core_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                phase TEXT NOT NULL DEFAULT 'idle',
                repair_cycle INTEGER NOT NULL DEFAULT 0,
                spec_json TEXT NOT NULL DEFAULT '{}',
                latest_acceptance_json TEXT NOT NULL DEFAULT '{}',
                latest_failure_fingerprint TEXT,
                last_error TEXT,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_phase_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                duration_ms INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_phase_events_run
            ON agent_v3_phase_events(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                purpose TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                input_chars INTEGER NOT NULL DEFAULT 0,
                output_chars INTEGER NOT NULL DEFAULT 0,
                prompt_budget_chars INTEGER NOT NULL DEFAULT 0,
                context_size INTEGER NOT NULL DEFAULT 0,
                total_timeout_seconds INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_model_calls_run
            ON agent_v3_model_calls(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_protocol_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                purpose TEXT NOT NULL,
                model TEXT,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                schema_name TEXT,
                raw_output_chars INTEGER NOT NULL DEFAULT 0,
                raw_preview TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_protocol_events_run
            ON agent_v3_protocol_events(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_repair_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                repair_number INTEGER NOT NULL,
                model TEXT,
                hypothesis TEXT,
                changed_files_json TEXT NOT NULL DEFAULT '[]',
                before_fingerprint TEXT,
                after_fingerprint TEXT,
                progress_class TEXT NOT NULL,
                score_delta INTEGER NOT NULL DEFAULT 0,
                before_evidence_json TEXT NOT NULL DEFAULT '{}',
                after_evidence_json TEXT NOT NULL DEFAULT '{}',
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_repair_outcomes_run
            ON agent_v3_repair_outcomes(run_id, id)
            """
        )
        # v3.9: repair outcomes are scoped to lifecycle campaigns.  Existing
        # rows predate campaigns and belong to the baseline campaign.
        cur.execute("PRAGMA table_info(agent_v3_repair_outcomes)")
        repair_columns = {str(row[1]) for row in cur.fetchall()}
        if "campaign_key" not in repair_columns:
            cur.execute(
                "ALTER TABLE agent_v3_repair_outcomes "
                "ADD COLUMN campaign_key TEXT NOT NULL DEFAULT 'baseline'"
            )
        if "campaign_repair_number" not in repair_columns:
            cur.execute(
                "ALTER TABLE agent_v3_repair_outcomes "
                "ADD COLUMN campaign_repair_number INTEGER NOT NULL DEFAULT 0"
            )
        cur.execute(
            """
            UPDATE agent_v3_repair_outcomes
            SET campaign_key = 'baseline'
            WHERE campaign_key IS NULL OR TRIM(campaign_key) = ''
            """
        )
        cur.execute(
            """
            UPDATE agent_v3_repair_outcomes
            SET campaign_repair_number = repair_number
            WHERE campaign_repair_number IS NULL OR campaign_repair_number <= 0
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_repair_outcomes_campaign
            ON agent_v3_repair_outcomes(run_id, campaign_key, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_candidate_validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                dependencies_changed INTEGER NOT NULL DEFAULT 0,
                command_text TEXT,
                status TEXT,
                exit_code INTEGER,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                progress_class TEXT,
                detail TEXT,
                stdout_text TEXT,
                stderr_text TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_candidate_validations_run
            ON agent_v3_candidate_validations(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_budget_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                grant_type TEXT NOT NULL,
                phase TEXT NOT NULL,
                steps INTEGER NOT NULL,
                reason TEXT,
                ceiling_before INTEGER NOT NULL,
                ceiling_after INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_budget_grants_run
            ON agent_v3_budget_grants(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_acceptance_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                satisfied INTEGER NOT NULL DEFAULT 0,
                user_deliverable_satisfied INTEGER NOT NULL DEFAULT 0,
                execution_satisfied INTEGER NOT NULL DEFAULT 0,
                platform_satisfied INTEGER NOT NULL DEFAULT 0,
                repairable_issue_count INTEGER NOT NULL DEFAULT 0,
                ignored_unknown_ids_json TEXT NOT NULL DEFAULT '[]',
                platform_evidence_json TEXT NOT NULL DEFAULT '{}',
                acceptance_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_acceptance_evaluations_run
            ON agent_v3_acceptance_evaluations(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_dependency_resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                package_name TEXT NOT NULL,
                requested_spec TEXT,
                effective_spec TEXT,
                provenance TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_dependency_resolutions_run
            ON agent_v3_dependency_resolutions(run_id, id)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_v3_demonstration_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                detail TEXT,
                execution_status TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_v3_demonstration_events_run
            ON agent_v3_demonstration_events(run_id, id)
            """
        )
        conn.commit()
        conn.close()
        _STORAGE_READY = True


def ensure_v3_run(run):
    initialize_v3_storage()
    now = utc_iso()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_runs (
            run_id, user_id, core_version, status, phase, repair_cycle,
            spec_json, latest_acceptance_json, latest_failure_fingerprint,
            last_error, started_at, updated_at
        ) VALUES (?, ?, ?, 'idle', 'idle', 0, '{}', '{}', NULL, NULL, NULL, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            user_id = excluded.user_id,
            core_version = excluded.core_version,
            updated_at = excluded.updated_at
        """,
        (str(run["id"]), int(run["user_id"]), CORE_VERSION, now),
    )
    conn.commit()
    conn.close()
    return get_v3_run(run["user_id"], run["id"])


def get_v3_run(user_id, run_id):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT run_id, user_id, core_version, status, phase, repair_cycle,
               spec_json, latest_acceptance_json, latest_failure_fingerprint,
               last_error, started_at, updated_at
        FROM agent_v3_runs
        WHERE run_id = ? AND user_id = ?
        """,
        (str(run_id), int(user_id)),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "run_id": row[0],
        "user_id": int(row[1]),
        "core_version": row[2],
        "status": row[3],
        "phase": row[4],
        "repair_cycle": int(row[5] or 0),
        "spec": _loads(row[6], {}),
        "acceptance": _loads(row[7], {}),
        "latest_failure_fingerprint": row[8],
        "last_error": row[9],
        "started_at": row[10],
        "updated_at": row[11],
    }


def update_v3_run(
    run,
    *,
    status=None,
    phase=None,
    repair_cycle=None,
    spec=None,
    acceptance=None,
    latest_failure_fingerprint=None,
    last_error=None,
    started=False,
):
    ensure_v3_run(run)
    current = get_v3_run(run["user_id"], run["id"]) or {}
    now = utc_iso()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE agent_v3_runs
        SET status = ?, phase = ?, repair_cycle = ?, spec_json = ?,
            latest_acceptance_json = ?, latest_failure_fingerprint = ?,
            last_error = ?, started_at = ?, updated_at = ?
        WHERE run_id = ? AND user_id = ?
        """,
        (
            str(status if status is not None else current.get("status") or "idle"),
            str(phase if phase is not None else current.get("phase") or "idle"),
            int(repair_cycle if repair_cycle is not None else current.get("repair_cycle") or 0),
            _json(spec if spec is not None else current.get("spec") or {}),
            _json(acceptance if acceptance is not None else current.get("acceptance") or {}),
            latest_failure_fingerprint if latest_failure_fingerprint is not None else current.get("latest_failure_fingerprint"),
            None if last_error is None else str(last_error)[:6000],
            now if started and not current.get("started_at") else current.get("started_at"),
            now,
            str(run["id"]),
            int(run["user_id"]),
        ),
    )
    conn.commit()
    conn.close()
    return get_v3_run(run["user_id"], run["id"])


def record_phase_event(run, phase, status, detail=None, duration_ms=None):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_phase_events (
            run_id, user_id, phase, status, detail, duration_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            str(phase)[:80],
            str(status)[:40],
            str(detail or "")[:4000] or None,
            None if duration_ms is None else int(duration_ms),
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()


def record_model_call(
    run,
    *,
    phase,
    purpose,
    model,
    status,
    duration_ms,
    input_chars,
    output_chars,
    prompt_budget_chars,
    context_size,
    total_timeout_seconds,
    error=None,
):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_model_calls (
            run_id, user_id, phase, purpose, model, status, duration_ms,
            input_chars, output_chars, prompt_budget_chars, context_size,
            total_timeout_seconds, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            str(phase)[:80],
            str(purpose)[:120],
            str(model)[:255],
            str(status)[:40],
            int(duration_ms or 0),
            int(input_chars or 0),
            int(output_chars or 0),
            int(prompt_budget_chars or 0),
            int(context_size or 0),
            int(total_timeout_seconds or 0),
            str(error or "")[:4000] or None,
            utc_iso(),
        ),
    )
    conn.commit()
    conn.close()


def list_model_calls(user_id, run_id, limit=50):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT phase, purpose, model, status, duration_ms, input_chars,
               output_chars, prompt_budget_chars, context_size,
               total_timeout_seconds, error, created_at
        FROM agent_v3_model_calls
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(200, int(limit)))),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "phase": row[0], "purpose": row[1], "model": row[2], "status": row[3],
            "duration_ms": int(row[4] or 0), "input_chars": int(row[5] or 0),
            "output_chars": int(row[6] or 0), "prompt_budget_chars": int(row[7] or 0),
            "context_size": int(row[8] or 0), "total_timeout_seconds": int(row[9] or 0),
            "error": row[10], "created_at": row[11],
        }
        for row in rows
    ]


def record_protocol_event(
    run,
    *,
    phase,
    purpose,
    model=None,
    event_type,
    status,
    schema_name=None,
    raw_output_chars=0,
    raw_preview=None,
    detail=None,
):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_protocol_events (
            run_id, user_id, phase, purpose, model, event_type, status,
            schema_name, raw_output_chars, raw_preview, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]), int(run["user_id"]), str(phase)[:80],
            str(purpose)[:120], str(model or "")[:255] or None,
            str(event_type)[:80], str(status)[:40],
            str(schema_name or "")[:120] or None, int(raw_output_chars or 0),
            str(raw_preview or "")[:5000] or None, str(detail or "")[:4000] or None,
            utc_iso(),
        ),
    )
    conn.commit(); conn.close()


def list_protocol_events(user_id, run_id, limit=100):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        SELECT phase, purpose, model, event_type, status, schema_name,
               raw_output_chars, raw_preview, detail, created_at
        FROM agent_v3_protocol_events
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(300, int(limit)))),
    )
    rows = cur.fetchall(); conn.close()
    return [{
        "phase": r[0], "purpose": r[1], "model": r[2], "event_type": r[3],
        "status": r[4], "schema_name": r[5], "raw_output_chars": int(r[6] or 0),
        "raw_preview": r[7], "detail": r[8], "created_at": r[9],
    } for r in rows]


def list_phase_events(user_id, run_id, limit=200):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT phase, status, detail, duration_ms, created_at
        FROM agent_v3_phase_events
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(500, int(limit)))),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "phase": row[0],
            "status": row[1],
            "detail": row[2],
            "duration_ms": None if row[3] is None else int(row[3]),
            "created_at": row[4],
        }
        for row in rows
    ]


def record_repair_outcome(
    run,
    *,
    repair_number,
    model=None,
    hypothesis=None,
    changed_files=None,
    progress=None,
    campaign_key="baseline",
    campaign_repair_number=None,
):
    initialize_v3_storage()
    progress = dict(progress or {})
    before = dict(progress.get("before") or {})
    after = dict(progress.get("after") or {})
    campaign = str(campaign_key or "baseline").strip().lower() or "baseline"
    campaign_number = int(campaign_repair_number or repair_number or 0)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_repair_outcomes (
            run_id, user_id, repair_number, model, hypothesis,
            changed_files_json, before_fingerprint, after_fingerprint,
            progress_class, score_delta, before_evidence_json,
            after_evidence_json, detail, created_at, campaign_key,
            campaign_repair_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]),
            int(run["user_id"]),
            int(repair_number),
            str(model or "")[:255] or None,
            str(hypothesis or "")[:3000] or None,
            json.dumps(list(changed_files or []), ensure_ascii=False),
            before.get("fingerprint"),
            after.get("fingerprint"),
            str(progress.get("classification") or "unknown")[:80],
            int(progress.get("score_delta") or 0),
            json.dumps(before, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(after, ensure_ascii=False, sort_keys=True, default=str),
            str(progress.get("reason") or "")[:4000] or None,
            utc_iso(),
            campaign[:80],
            campaign_number,
        ),
    )
    conn.commit()
    conn.close()


def list_repair_outcomes(user_id, run_id, limit=100, campaign_key=None):
    initialize_v3_storage()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT repair_number, model, hypothesis, changed_files_json,
               before_fingerprint, after_fingerprint, progress_class,
               score_delta, before_evidence_json, after_evidence_json,
               detail, created_at, campaign_key, campaign_repair_number
        FROM agent_v3_repair_outcomes
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(300, int(limit)))),
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    requested_campaign = (str(campaign_key).strip().lower() if campaign_key is not None else None)
    for row in rows:
        campaign = str(row[12] or "baseline").strip().lower() or "baseline"
        if requested_campaign is not None and campaign != requested_campaign:
            continue
        result.append({
            "repair_number": int(row[0] or 0),
            "model": row[1],
            "hypothesis": row[2],
            "changed_files": _loads(row[3], []),
            "before_fingerprint": row[4],
            "after_fingerprint": row[5],
            "progress_class": row[6],
            "score_delta": int(row[7] or 0),
            "before": _loads(row[8], {}),
            "after": _loads(row[9], {}),
            "detail": row[10],
            "created_at": row[11],
            "campaign_key": campaign,
            "campaign_repair_number": int(row[13] or row[0] or 0),
        })
    return result



def record_candidate_validation(run, result):
    initialize_v3_storage()
    result = dict(result or {})
    execution = dict(result.get("execution") or {})
    progress = dict(result.get("progress") or {})
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_candidate_validations (
            run_id, user_id, purpose, accepted, dependencies_changed,
            command_text, status, exit_code, duration_ms, progress_class,
            detail, stdout_text, stderr_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]), int(run["user_id"]),
            str(result.get("purpose") or "unknown")[:80],
            int(bool(result.get("accepted"))),
            int(bool(result.get("dependencies_changed"))),
            str(execution.get("command") or "")[:1000] or None,
            str(execution.get("status") or "")[:40] or None,
            execution.get("exit_code"), int(execution.get("duration_ms") or 0),
            str(progress.get("classification") or "")[:80] or None,
            str(result.get("detail") or "")[:4000] or None,
            str(execution.get("stdout") or "")[-12000:],
            str(execution.get("stderr") or "")[-12000:], utc_iso(),
        ),
    )
    conn.commit(); conn.close()


def list_candidate_validations(user_id, run_id, limit=100):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        SELECT purpose, accepted, dependencies_changed, command_text, status,
               exit_code, duration_ms, progress_class, detail,
               stdout_text, stderr_text, created_at
        FROM agent_v3_candidate_validations
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(300, int(limit)))),
    )
    rows = cur.fetchall(); conn.close()
    return [{
        "purpose": r[0], "accepted": bool(r[1]), "dependencies_changed": bool(r[2]),
        "command": r[3], "status": r[4], "exit_code": r[5],
        "duration_ms": int(r[6] or 0), "progress_class": r[7], "detail": r[8],
        "stdout": r[9], "stderr": r[10], "created_at": r[11],
    } for r in rows]


def record_budget_grant(run, *, grant_type, phase, steps, reason, ceiling_before, ceiling_after):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_budget_grants (
            run_id, user_id, grant_type, phase, steps, reason,
            ceiling_before, ceiling_after, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(run["id"]), int(run["user_id"]), str(grant_type)[:80], str(phase)[:80],
         int(steps), str(reason or "")[:3000] or None, int(ceiling_before), int(ceiling_after), utc_iso()),
    )
    conn.commit(); conn.close()


def list_budget_grants(user_id, run_id, limit=100):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        SELECT grant_type, phase, steps, reason, ceiling_before,
               ceiling_after, created_at
        FROM agent_v3_budget_grants
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(300, int(limit)))),
    )
    rows=cur.fetchall(); conn.close()
    return [{"grant_type": r[0], "phase": r[1], "steps": int(r[2] or 0), "reason": r[3],
             "ceiling_before": int(r[4] or 0), "ceiling_after": int(r[5] or 0), "created_at": r[6]} for r in rows]


def total_tail_steps(user_id, run_id):
    return sum(item.get("steps", 0) for item in list_budget_grants(user_id, run_id, limit=300)
               if item.get("grant_type") == "progress_tail")


def record_acceptance_evaluation(run, acceptance):
    initialize_v3_storage()
    acceptance = dict(acceptance or {})
    layers = dict(acceptance.get("layer_summary") or {})
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_acceptance_evaluations (
            run_id, user_id, satisfied,
            user_deliverable_satisfied, execution_satisfied, platform_satisfied,
            repairable_issue_count, ignored_unknown_ids_json,
            platform_evidence_json, acceptance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]), int(run["user_id"]), int(bool(acceptance.get("satisfied"))),
            int(bool(layers.get("user_deliverable"))),
            int(bool(layers.get("execution"))),
            int(bool(layers.get("platform"))),
            len(acceptance.get("repairable_issues") or []),
            json.dumps(acceptance.get("ignored_unknown_model_ids") or [], ensure_ascii=False),
            json.dumps(acceptance.get("platform_evidence") or {}, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(acceptance, ensure_ascii=False, sort_keys=True, default=str),
            utc_iso(),
        ),
    )
    conn.commit(); conn.close()


def list_acceptance_evaluations(user_id, run_id, limit=100):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        SELECT satisfied, user_deliverable_satisfied, execution_satisfied,
               platform_satisfied, repairable_issue_count,
               ignored_unknown_ids_json, platform_evidence_json,
               acceptance_json, created_at
        FROM agent_v3_acceptance_evaluations
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(300, int(limit)))),
    )
    rows = cur.fetchall(); conn.close()
    return [{
        "satisfied": bool(r[0]),
        "user_deliverable_satisfied": bool(r[1]),
        "execution_satisfied": bool(r[2]),
        "platform_satisfied": bool(r[3]),
        "repairable_issue_count": int(r[4] or 0),
        "ignored_unknown_model_ids": _loads(r[5], []),
        "platform_evidence": _loads(r[6], {}),
        "acceptance": _loads(r[7], {}),
        "created_at": r[8],
    } for r in rows]


def record_dependency_resolutions(run, resolutions):
    initialize_v3_storage()
    items = [dict(item) for item in (resolutions or []) if isinstance(item, dict)]
    if not items:
        return
    conn = get_connection(); cur = conn.cursor()
    for item in items:
        cur.execute(
            """
            INSERT INTO agent_v3_dependency_resolutions (
                run_id, user_id, package_name, requested_spec, effective_spec,
                provenance, status, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(run["id"]), int(run["user_id"]),
                str(item.get("package") or "")[:255],
                str(item.get("requested_spec") or "")[:255] or None,
                str(item.get("effective_spec") or "")[:255] or None,
                str(item.get("provenance") or "unknown")[:80],
                str(item.get("status") or "unknown")[:80],
                str(item.get("detail") or "")[:3000] or None,
                utc_iso(),
            ),
        )
    conn.commit(); conn.close()


def list_dependency_resolutions(user_id, run_id, limit=200):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        SELECT package_name, requested_spec, effective_spec, provenance, status, detail, created_at
        FROM agent_v3_dependency_resolutions
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(500, int(limit)))),
    )
    rows = cur.fetchall(); conn.close()
    return [{
        "package": row[0], "requested_spec": row[1], "effective_spec": row[2],
        "provenance": row[3], "status": row[4], "detail": row[5], "created_at": row[6],
    } for row in rows]


def record_demonstration_event(run, event_type, *, detail=None, execution_status=None):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO agent_v3_demonstration_events (
            run_id, user_id, event_type, detail, execution_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(run["id"]), int(run["user_id"]), str(event_type)[:80],
            str(detail or "")[:3000] or None,
            str(execution_status or "")[:80] or None,
            utc_iso(),
        ),
    )
    conn.commit(); conn.close()


def list_demonstration_events(user_id, run_id, limit=100):
    initialize_v3_storage()
    conn = get_connection(); cur = conn.cursor()
    cur.execute(
        """
        SELECT event_type, detail, execution_status, created_at
        FROM agent_v3_demonstration_events
        WHERE run_id = ? AND user_id = ?
        ORDER BY id ASC LIMIT ?
        """,
        (str(run_id), int(user_id), max(1, min(300, int(limit)))),
    )
    rows = cur.fetchall(); conn.close()
    return [{
        "event_type": row[0], "detail": row[1], "execution_status": row[2], "created_at": row[3],
    } for row in rows]


def demonstration_status(user_id, run_id):
    events = list_demonstration_events(user_id, run_id, limit=300)
    types = [str(item.get("event_type") or "") for item in events]
    return {
        "baseline_verified": "baseline_verified" in types,
        "defect_injected": "defect_injected" in types,
        "failure_observed": "failure_observed" in types,
        "repair_verified": "repair_verified" in types,
        "satisfied": all(name in types for name in ("baseline_verified", "defect_injected", "failure_observed", "repair_verified")),
        "events": events,
    }


def diagnostics_snapshot(user_id, run_id):
    """Structured v3 diagnostics for future API/UI/export wiring.

    Control-plane telemetry stays outside the mutable project workspace.
    Sandbox history is joined lazily here so storage initialization does not
    create a module import cycle.
    """
    try:
        from app.services.agent_sandbox import list_agent_sandbox_executions
        executions = list_agent_sandbox_executions(user_id, run_id, limit=200)
    except Exception:
        executions = []
    return {
        "run": get_v3_run(user_id, run_id),
        "phase_events": list_phase_events(user_id, run_id, limit=300),
        "model_calls": list_model_calls(user_id, run_id, limit=200),
        "protocol_events": list_protocol_events(user_id, run_id, limit=300),
        "repair_outcomes": list_repair_outcomes(user_id, run_id, limit=200),
        "candidate_validations": list_candidate_validations(user_id, run_id, limit=200),
        "budget_grants": list_budget_grants(user_id, run_id, limit=200),
        "acceptance_evaluations": list_acceptance_evaluations(user_id, run_id, limit=200),
        "dependency_resolutions": list_dependency_resolutions(user_id, run_id, limit=300),
        "demonstration": demonstration_status(user_id, run_id),
        "sandbox_executions": executions,
    }
