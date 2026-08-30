import json
import math
import secrets

from datetime import (
    date,
    datetime,
    time,
    timedelta,
    timezone,
)
from zoneinfo import (
    ZoneInfo,
    ZoneInfoNotFoundError,
)

from app.database import (
    get_connection,
)


AUTOMATION_PERMISSION = "automation.use"
VALID_TASK_TYPES = {
    "reminder",
    "ai",
    "condition",
}
VALID_SCHEDULE_KINDS = {
    "once",
    "interval",
    "daily",
    "weekly",
}
VALID_MODEL_MODES = {
    "auto",
    "fast",
    "default",
    "deep",
}
VALID_INTERVAL_UNITS = {
    "minutes": 60,
    "hours": 60 * 60,
}


class AutomationStoreError(Exception):
    pass


def utc_now():
    return datetime.now(
        timezone.utc
    )


def utc_iso(value=None):
    value = value or utc_now()

    if value.tzinfo is None:
        value = value.replace(
            tzinfo=timezone.utc
        )

    return (
        value.astimezone(timezone.utc)
        .isoformat()
    )


def parse_utc(value):
    text = str(value or "").strip()

    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return parsed.astimezone(
        timezone.utc
    )


def _safe_json_load(value, default=None):
    if default is None:
        default = {}

    if isinstance(value, dict):
        return dict(value)

    try:
        parsed = json.loads(
            value or "{}"
        )
    except (
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return dict(default)

    return (
        parsed
        if isinstance(parsed, dict)
        else dict(default)
    )


def _serialize_json(value):
    return json.dumps(
        value or {},
        ensure_ascii=False,
        sort_keys=True,
    )


def _timezone(value):
    name = str(
        value or "UTC"
    ).strip()

    try:
        return ZoneInfo(name), name
    except ZoneInfoNotFoundError as error:
        raise AutomationStoreError(
            f"Unknown timezone: {name}"
        ) from error


def _parse_date(value, field_name):
    try:
        return date.fromisoformat(
            str(value or "").strip()
        )
    except ValueError as error:
        raise AutomationStoreError(
            f"Invalid {field_name}."
        ) from error


def _parse_time(value, field_name):
    text = str(value or "").strip()

    try:
        parsed = time.fromisoformat(text)
    except ValueError as error:
        raise AutomationStoreError(
            f"Invalid {field_name}."
        ) from error

    return parsed.replace(
        second=0,
        microsecond=0,
    )


def _parse_local_datetime(value, tz, field_name):
    text = str(value or "").strip()

    try:
        parsed = datetime.fromisoformat(
            text
        )
    except ValueError as error:
        raise AutomationStoreError(
            f"Invalid {field_name}."
        ) from error

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=tz
        )
    else:
        parsed = parsed.astimezone(tz)

    return parsed.replace(
        second=0,
        microsecond=0,
    )


def _normalize_title(value):
    title = " ".join(
        str(value or "").split()
    )

    if not title:
        raise AutomationStoreError(
            "Task title is required."
        )

    if len(title) > 120:
        raise AutomationStoreError(
            "Task title is too long."
        )

    return title


def _normalize_instruction(value):
    instruction = str(
        value or ""
    ).strip()

    if not instruction:
        raise AutomationStoreError(
            "Task instruction is required."
        )

    if len(instruction) > 6000:
        raise AutomationStoreError(
            "Task instruction is too long."
        )

    return instruction


def _normalize_condition(value, task_type):
    condition = str(
        value or ""
    ).strip()

    if task_type != "condition":
        return None

    if not condition:
        raise AutomationStoreError(
            "Condition tasks need a notification condition."
        )

    if len(condition) > 3000:
        raise AutomationStoreError(
            "Condition text is too long."
        )

    return condition


def normalize_schedule(
    schedule_kind,
    schedule,
    timezone_name,
    now=None,
):
    schedule_kind = str(
        schedule_kind or ""
    ).strip().lower()

    if schedule_kind not in VALID_SCHEDULE_KINDS:
        raise AutomationStoreError(
            "Unsupported schedule type."
        )

    schedule = _safe_json_load(
        schedule,
        {},
    )

    tz, timezone_name = _timezone(
        timezone_name
    )

    now = now or utc_now()
    now_local = now.astimezone(tz)

    if schedule_kind == "once":
        run_at = _parse_local_datetime(
            schedule.get("run_at_local"),
            tz,
            "run time",
        )

        if run_at <= now_local:
            raise AutomationStoreError(
                "One-time tasks must be scheduled in the future."
            )

        normalized = {
            "run_at_local": (
                run_at.replace(tzinfo=None)
                .isoformat(timespec="minutes")
            ),
        }

    elif schedule_kind == "interval":
        try:
            every = int(
                schedule.get("every", 1)
            )
        except (
            TypeError,
            ValueError,
        ) as error:
            raise AutomationStoreError(
                "Invalid interval value."
            ) from error

        unit = str(
            schedule.get("unit", "minutes")
        ).strip().lower()

        if unit not in VALID_INTERVAL_UNITS:
            raise AutomationStoreError(
                "Interval unit must be minutes or hours."
            )

        if every < 1 or every > 10080:
            raise AutomationStoreError(
                "Interval value is outside the supported range."
            )

        first_run_raw = schedule.get(
            "first_run_local"
        )

        if first_run_raw:
            first_run = _parse_local_datetime(
                first_run_raw,
                tz,
                "first run time",
            )
        else:
            first_run = (
                now_local
                + timedelta(minutes=1)
            ).replace(
                second=0,
                microsecond=0,
            )

        normalized = {
            "every": every,
            "unit": unit,
            "anchor_utc": utc_iso(
                first_run.astimezone(
                    timezone.utc
                )
            ),
            "anchor_local": (
                first_run.replace(tzinfo=None)
                .isoformat(timespec="minutes")
            ),
        }

    elif schedule_kind == "daily":
        try:
            every = int(
                schedule.get("every", 1)
            )
        except (
            TypeError,
            ValueError,
        ) as error:
            raise AutomationStoreError(
                "Invalid daily interval."
            ) from error

        if every < 1 or every > 30:
            raise AutomationStoreError(
                "Daily interval must be between 1 and 30 days."
            )

        run_time = _parse_time(
            schedule.get("time"),
            "daily time",
        )

        anchor_date = (
            _parse_date(
                schedule.get("anchor_date"),
                "daily start date",
            )
            if schedule.get("anchor_date")
            else now_local.date()
        )

        normalized = {
            "every": every,
            "time": run_time.isoformat(
                timespec="minutes"
            ),
            "anchor_date": (
                anchor_date.isoformat()
            ),
        }

    else:
        try:
            every = int(
                schedule.get("every", 1)
            )
        except (
            TypeError,
            ValueError,
        ) as error:
            raise AutomationStoreError(
                "Invalid weekly interval."
            ) from error

        if every < 1 or every > 12:
            raise AutomationStoreError(
                "Weekly interval must be between 1 and 12 weeks."
            )

        try:
            weekday = int(
                schedule.get("weekday")
            )
        except (
            TypeError,
            ValueError,
        ) as error:
            raise AutomationStoreError(
                "Invalid weekday."
            ) from error

        if weekday < 0 or weekday > 6:
            raise AutomationStoreError(
                "Weekday must be between Monday and Sunday."
            )

        run_time = _parse_time(
            schedule.get("time"),
            "weekly time",
        )

        anchor_date = (
            _parse_date(
                schedule.get("anchor_date"),
                "weekly start date",
            )
            if schedule.get("anchor_date")
            else now_local.date()
        )

        normalized = {
            "every": every,
            "weekday": weekday,
            "time": run_time.isoformat(
                timespec="minutes"
            ),
            "anchor_date": (
                anchor_date.isoformat()
            ),
        }

    return (
        schedule_kind,
        normalized,
        timezone_name,
    )


def compute_next_run(
    schedule_kind,
    schedule,
    timezone_name,
    after=None,
):
    schedule_kind = str(
        schedule_kind or ""
    ).strip().lower()
    schedule = _safe_json_load(
        schedule,
        {},
    )
    tz, _ = _timezone(timezone_name)

    after = after or utc_now()
    if after.tzinfo is None:
        after = after.replace(
            tzinfo=timezone.utc
        )
    after = after.astimezone(
        timezone.utc
    )

    if schedule_kind == "once":
        local = _parse_local_datetime(
            schedule.get("run_at_local"),
            tz,
            "run time",
        )
        candidate = local.astimezone(
            timezone.utc
        )
        return candidate if candidate > after else None

    if schedule_kind == "interval":
        anchor = parse_utc(
            schedule.get("anchor_utc")
        )
        if not anchor:
            raise AutomationStoreError(
                "Interval schedule is missing its anchor."
            )

        every = int(
            schedule.get("every", 1)
        )
        unit = str(
            schedule.get("unit", "minutes")
        ).lower()
        seconds = (
            every
            * VALID_INTERVAL_UNITS[
                unit
            ]
        )

        if anchor > after:
            return anchor

        elapsed = (
            after - anchor
        ).total_seconds()
        jumps = (
            math.floor(
                elapsed / seconds
            )
            + 1
        )
        return (
            anchor
            + timedelta(
                seconds=jumps * seconds
            )
        )

    after_local = after.astimezone(tz)
    every = int(
        schedule.get("every", 1)
    )
    run_time = _parse_time(
        schedule.get("time"),
        "scheduled time",
    )
    anchor_date = _parse_date(
        schedule.get("anchor_date"),
        "schedule start date",
    )

    if schedule_kind == "daily":
        for offset in range(0, 3660):
            candidate_date = (
                after_local.date()
                + timedelta(days=offset)
            )

            if candidate_date < anchor_date:
                continue

            day_delta = (
                candidate_date
                - anchor_date
            ).days

            if day_delta % every:
                continue

            candidate_local = datetime.combine(
                candidate_date,
                run_time,
                tzinfo=tz,
            )

            candidate = candidate_local.astimezone(
                timezone.utc
            )

            if candidate > after:
                return candidate

        return None

    if schedule_kind == "weekly":
        weekday = int(
            schedule.get("weekday")
        )
        anchor_monday = (
            anchor_date
            - timedelta(
                days=anchor_date.weekday()
            )
        )

        for offset in range(0, 3660):
            candidate_date = (
                after_local.date()
                + timedelta(days=offset)
            )

            if (
                candidate_date.weekday()
                != weekday
            ):
                continue

            candidate_monday = (
                candidate_date
                - timedelta(
                    days=candidate_date.weekday()
                )
            )

            weeks = (
                candidate_monday
                - anchor_monday
            ).days // 7

            if weeks < 0 or weeks % every:
                continue

            candidate_local = datetime.combine(
                candidate_date,
                run_time,
                tzinfo=tz,
            )
            candidate = candidate_local.astimezone(
                timezone.utc
            )

            if candidate > after:
                return candidate

        return None

    raise AutomationStoreError(
        "Unsupported schedule type."
    )


def initialize_automation_storage():
    conn = get_connection()
    cursor = conn.cursor()
    timestamp = utc_iso()

    # Capability permission. Kept additive so existing databases are preserved.
    cursor.execute(
        """
        INSERT OR IGNORE INTO permissions (
            name,
            description,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            AUTOMATION_PERMISSION,
            "Create and manage personal automations.",
            timestamp,
            timestamp,
        ),
    )

    cursor.execute(
        "SELECT id FROM permissions WHERE name = ?",
        (AUTOMATION_PERMISSION,),
    )
    permission_row = cursor.fetchone()

    if permission_row:
        permission_id = permission_row[0]
        cursor.execute(
            """
            SELECT id, name
            FROM roles
            WHERE name IN ('owner', 'admin', 'user')
            """
        )

        for role_id, _ in cursor.fetchall():
            cursor.execute(
                """
                INSERT OR IGNORE INTO role_permissions (
                    role_id,
                    permission_id,
                    granted_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    role_id,
                    permission_id,
                    timestamp,
                ),
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            task_type TEXT NOT NULL,
            instruction TEXT NOT NULL,
            condition_text TEXT,
            schedule_kind TEXT NOT NULL,
            schedule_json TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            model_mode TEXT NOT NULL DEFAULT 'default',
            allow_web INTEGER NOT NULL DEFAULT 0,
            allow_rag INTEGER NOT NULL DEFAULT 0,
            allow_memory INTEGER NOT NULL DEFAULT 0,
            notify_on_change INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'scheduled',
            next_run_at TEXT,
            last_run_at TEXT,
            last_result TEXT,
            last_error TEXT,
            last_condition_key TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            manual_run_requested INTEGER NOT NULL DEFAULT 0,
            locked_at TEXT,
            lock_token TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            cancel_requested_at TEXT,
            pause_after_cancel INTEGER NOT NULL DEFAULT 0,
            last_condition_met INTEGER,
            last_notified INTEGER NOT NULL DEFAULT 0,
            compiled_spec_json TEXT NOT NULL DEFAULT '{}',
            preflight_status TEXT NOT NULL DEFAULT 'legacy',
            preflight_message TEXT,
            preflight_updated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    # v1.8.1 additive task-compiler metadata for databases that already
    # created automation_tasks under v1.8.
    cursor.execute(
        "PRAGMA table_info(automation_tasks)"
    )
    task_columns = {
        row[1]
        for row in cursor.fetchall()
    }

    additive_columns = {
        "compiled_spec_json": (
            "TEXT NOT NULL DEFAULT '{}'"
        ),
        "preflight_status": (
            "TEXT NOT NULL DEFAULT 'legacy'"
        ),
        "preflight_message": "TEXT",
        "preflight_updated_at": "TEXT",
        "cancel_requested": (
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "cancel_requested_at": "TEXT",
        "pause_after_cancel": (
            "INTEGER NOT NULL DEFAULT 0"
        ),
        "last_condition_met": "INTEGER",
        "last_notified": (
            "INTEGER NOT NULL DEFAULT 0"
        ),
    }

    for column_name, column_sql in additive_columns.items():
        if column_name not in task_columns:
            cursor.execute(
                f"ALTER TABLE automation_tasks ADD COLUMN {column_name} {column_sql}"
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            user_id INTEGER NOT NULL,
            trigger_type TEXT NOT NULL,
            scheduled_for TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT,
            condition_met INTEGER,
            notified INTEGER NOT NULL DEFAULT 0,
            tool_log_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id)
                REFERENCES automation_tasks(id)
                ON DELETE SET NULL,
            FOREIGN KEY (user_id)
                REFERENCES users(id)
                ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_automation_tasks_due
        ON automation_tasks(
            state,
            enabled,
            manual_run_requested,
            next_run_at
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_automation_tasks_user
        ON automation_tasks(user_id, updated_at, id)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_automation_runs_user
        ON automation_runs(user_id, id)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_automation_runs_task
        ON automation_runs(task_id, id)
        """
    )

    conn.commit()
    conn.close()


def recover_stale_tasks():
    """
    Recover task locks left by a previous automation-engine process.

    This function is called only when a fresh engine starts. At that point any
    persisted running/cancelling state belongs to the previous process, so it
    is safe to recover immediately instead of waiting for an arbitrary timeout.
    """

    initialize_automation_storage()
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, user_id
        FROM automation_tasks
        WHERE state IN ('running', 'cancelling')
        """
    )
    stale_tasks = cursor.fetchall()

    if not stale_tasks:
        conn.close()
        return 0

    cursor.executemany(
        """
        UPDATE automation_tasks
        SET
            state = CASE
                WHEN enabled = 1 THEN 'scheduled'
                ELSE 'paused'
            END,
            locked_at = NULL,
            lock_token = NULL,
            cancel_requested = 0,
            cancel_requested_at = NULL,
            pause_after_cancel = 0,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        [
            (
                timestamp,
                int(task_id),
                int(user_id),
            )
            for task_id, user_id
            in stale_tasks
        ],
    )

    for task_id, user_id in stale_tasks:
        cursor.execute(
            """
            UPDATE automation_runs
            SET
                finished_at = ?,
                status = 'interrupted',
                error = COALESCE(
                    error,
                    'Run interrupted because the automation engine restarted.'
                )
            WHERE
                task_id = ?
                AND user_id = ?
                AND status = 'running'
            """,
            (
                timestamp,
                int(task_id),
                int(user_id),
            ),
        )

    conn.commit()
    conn.close()
    return len(stale_tasks)


def _task_from_row(row):
    if not row:
        return None

    return {
        "id": row[0],
        "user_id": row[1],
        "title": row[2],
        "task_type": row[3],
        "instruction": row[4],
        "condition_text": row[5],
        "schedule_kind": row[6],
        "schedule": _safe_json_load(
            row[7],
            {},
        ),
        "timezone": row[8],
        "model_mode": row[9],
        "allow_web": bool(row[10]),
        "allow_rag": bool(row[11]),
        "allow_memory": bool(row[12]),
        "notify_on_change": bool(row[13]),
        "enabled": bool(row[14]),
        "state": row[15],
        "next_run_at": row[16],
        "last_run_at": row[17],
        "last_result": row[18],
        "last_error": row[19],
        "last_condition_key": row[20],
        "consecutive_failures": int(
            row[21] or 0
        ),
        "manual_run_requested": bool(
            row[22]
        ),
        "locked_at": row[23],
        "lock_token": row[24],
        "cancel_requested": bool(row[25]),
        "cancel_requested_at": row[26],
        "pause_after_cancel": bool(row[27]),
        "last_condition_met": (
            None
            if row[28] is None
            else bool(row[28])
        ),
        "last_notified": bool(row[29]),
        "compiled_spec": _safe_json_load(
            row[30],
            {},
        ),
        "preflight_status": row[31] or "legacy",
        "preflight_message": row[32],
        "preflight_updated_at": row[33],
        "created_at": row[34],
        "updated_at": row[35],
    }


def _task_select_sql():
    return """
        SELECT
            id,
            user_id,
            title,
            task_type,
            instruction,
            condition_text,
            schedule_kind,
            schedule_json,
            timezone,
            model_mode,
            allow_web,
            allow_rag,
            allow_memory,
            notify_on_change,
            enabled,
            state,
            next_run_at,
            last_run_at,
            last_result,
            last_error,
            last_condition_key,
            consecutive_failures,
            manual_run_requested,
            locked_at,
            lock_token,
            cancel_requested,
            cancel_requested_at,
            pause_after_cancel,
            last_condition_met,
            last_notified,
            compiled_spec_json,
            preflight_status,
            preflight_message,
            preflight_updated_at,
            created_at,
            updated_at
        FROM automation_tasks
    """


def get_task(user_id, task_id):
    initialize_automation_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        _task_select_sql()
        + " WHERE id = ? AND user_id = ?",
        (
            int(task_id),
            int(user_id),
        ),
    )
    task = _task_from_row(
        cursor.fetchone()
    )
    conn.close()
    return task


def list_tasks(user_id):
    initialize_automation_storage()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        _task_select_sql()
        + " WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
        (int(user_id),),
    )
    tasks = [
        _task_from_row(row)
        for row in cursor.fetchall()
    ]
    conn.close()
    return tasks


def _normalize_task_fields(payload, existing=None):
    payload = dict(payload or {})
    existing = existing or {}

    task_type = str(
        payload.get(
            "task_type",
            existing.get(
                "task_type",
                "reminder",
            ),
        )
    ).strip().lower()

    if task_type not in VALID_TASK_TYPES:
        raise AutomationStoreError(
            "Unsupported automation type."
        )

    title = _normalize_title(
        payload.get(
            "title",
            existing.get("title"),
        )
    )
    instruction = _normalize_instruction(
        payload.get(
            "instruction",
            existing.get("instruction"),
        )
    )
    condition_text = _normalize_condition(
        payload.get(
            "condition_text",
            existing.get(
                "condition_text"
            ),
        ),
        task_type,
    )

    model_mode = str(
        payload.get(
            "model_mode",
            existing.get(
                "model_mode",
                "default",
            ),
        )
    ).strip().lower()

    if model_mode not in VALID_MODEL_MODES:
        raise AutomationStoreError(
            "Unsupported model mode."
        )

    timezone_name = str(
        payload.get(
            "timezone",
            existing.get(
                "timezone",
                "UTC",
            ),
        )
    ).strip() or "UTC"

    schedule_kind = payload.get(
        "schedule_kind",
        existing.get("schedule_kind"),
    )
    schedule = payload.get(
        "schedule",
        existing.get("schedule"),
    )

    (
        schedule_kind,
        schedule,
        timezone_name,
    ) = normalize_schedule(
        schedule_kind,
        schedule,
        timezone_name,
    )

    next_run = compute_next_run(
        schedule_kind,
        schedule,
        timezone_name,
        after=(
            utc_now()
            - timedelta(seconds=1)
        ),
    )

    if not next_run:
        raise AutomationStoreError(
            "The schedule does not have a future run time."
        )

    allow_web = bool(
        payload.get(
            "allow_web",
            existing.get(
                "allow_web",
                False,
            ),
        )
    )
    allow_rag = bool(
        payload.get(
            "allow_rag",
            existing.get(
                "allow_rag",
                False,
            ),
        )
    )
    allow_memory = bool(
        payload.get(
            "allow_memory",
            existing.get(
                "allow_memory",
                False,
            ),
        )
    )
    notify_on_change = bool(
        payload.get(
            "notify_on_change",
            existing.get(
                "notify_on_change",
                True,
            ),
        )
    )

    if task_type == "reminder":
        allow_web = False
        allow_rag = False
        allow_memory = False

    return {
        "title": title,
        "task_type": task_type,
        "instruction": instruction,
        "condition_text": condition_text,
        "schedule_kind": schedule_kind,
        "schedule": schedule,
        "timezone": timezone_name,
        "model_mode": model_mode,
        "allow_web": allow_web,
        "allow_rag": allow_rag,
        "allow_memory": allow_memory,
        "notify_on_change": (
            notify_on_change
            if task_type == "condition"
            else False
        ),
        "next_run_at": utc_iso(
            next_run
        ),
    }


def create_task(user_id, payload, preflight=None):
    initialize_automation_storage()
    data = _normalize_task_fields(
        payload
    )
    preflight = dict(preflight or {})
    compiled_spec = (
        preflight.get("compiled_spec")
        if isinstance(preflight.get("compiled_spec"), dict)
        else {}
    )
    preflight_status = str(
        preflight.get("status")
        or "legacy"
    )[:40]
    preflight_message = str(
        preflight.get("summary")
        or ""
    ).strip()[:3000] or None
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO automation_tasks (
            user_id,
            title,
            task_type,
            instruction,
            condition_text,
            schedule_kind,
            schedule_json,
            timezone,
            model_mode,
            allow_web,
            allow_rag,
            allow_memory,
            notify_on_change,
            enabled,
            state,
            next_run_at,
            compiled_spec_json,
            preflight_status,
            preflight_message,
            preflight_updated_at,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            1, 'scheduled', ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            int(user_id),
            data["title"],
            data["task_type"],
            data["instruction"],
            data["condition_text"],
            data["schedule_kind"],
            _serialize_json(
                data["schedule"]
            ),
            data["timezone"],
            data["model_mode"],
            int(data["allow_web"]),
            int(data["allow_rag"]),
            int(data["allow_memory"]),
            int(
                data["notify_on_change"]
            ),
            data["next_run_at"],
            _serialize_json(compiled_spec),
            preflight_status,
            preflight_message,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )


def update_task(user_id, task_id, payload, preflight=None):
    existing = get_task(
        user_id,
        task_id,
    )

    if not existing:
        raise AutomationStoreError(
            "Automation task was not found."
        )

    if existing["state"] in {"running", "cancelling"}:
        raise AutomationStoreError(
            "Wait for the current run to stop before editing this task."
        )

    data = _normalize_task_fields(
        payload,
        existing=existing,
    )
    preflight = dict(preflight or {})
    compiled_spec = (
        preflight.get("compiled_spec")
        if isinstance(preflight.get("compiled_spec"), dict)
        else existing.get("compiled_spec") or {}
    )
    preflight_status = str(
        preflight.get("status")
        or existing.get("preflight_status")
        or "legacy"
    )[:40]
    preflight_message = str(
        preflight.get("summary")
        or ""
    ).strip()[:3000] or None

    # Saving a corrected task after a needs-input pause reactivates it.
    enabled = (
        True
        if existing.get("state") == "needs_input"
        else bool(existing["enabled"])
    )
    state = (
        "scheduled"
        if enabled
        else "paused"
    )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            title = ?,
            task_type = ?,
            instruction = ?,
            condition_text = ?,
            schedule_kind = ?,
            schedule_json = ?,
            timezone = ?,
            model_mode = ?,
            allow_web = ?,
            allow_rag = ?,
            allow_memory = ?,
            notify_on_change = ?,
            enabled = ?,
            state = ?,
            next_run_at = ?,
            compiled_spec_json = ?,
            preflight_status = ?,
            preflight_message = ?,
            preflight_updated_at = ?,
            last_result = NULL,
            last_error = NULL,
            last_condition_key = NULL,
            consecutive_failures = 0,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            data["title"],
            data["task_type"],
            data["instruction"],
            data["condition_text"],
            data["schedule_kind"],
            _serialize_json(
                data["schedule"]
            ),
            data["timezone"],
            data["model_mode"],
            int(data["allow_web"]),
            int(data["allow_rag"]),
            int(data["allow_memory"]),
            int(
                data["notify_on_change"]
            ),
            int(enabled),
            state,
            data["next_run_at"],
            _serialize_json(compiled_spec),
            preflight_status,
            preflight_message,
            utc_iso(),
            utc_iso(),
            int(task_id),
            int(user_id),
        ),
    )

    if cursor.rowcount != 1:
        conn.close()
        raise AutomationStoreError(
            "Automation task was not found."
        )

    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )


def delete_task(user_id, task_id):
    task = get_task(
        user_id,
        task_id,
    )

    if not task:
        return False

    if task["state"] in {"running", "cancelling"}:
        raise AutomationStoreError(
            "A running automation cannot be deleted. Stop it first."
        )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        DELETE FROM automation_tasks
        WHERE id = ? AND user_id = ?
        """,
        (
            int(task_id),
            int(user_id),
        ),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def set_task_enabled(
    user_id,
    task_id,
    enabled,
):
    task = get_task(
        user_id,
        task_id,
    )

    if not task:
        raise AutomationStoreError(
            "Automation task was not found."
        )

    if task["state"] in {"running", "cancelling"}:
        raise AutomationStoreError(
            "Use Stop run or Stop & pause while this automation is running."
        )

    if task["state"] == "needs_input" and bool(enabled):
        raise AutomationStoreError(
            "Edit this automation to resolve the requested clarification before resuming it."
        )

    enabled = bool(enabled)
    next_run_at = task["next_run_at"]

    if enabled:
        current_next = parse_utc(
            next_run_at
        )

        if (
            current_next is None
            or current_next <= utc_now()
        ):
            next_run = compute_next_run(
                task["schedule_kind"],
                task["schedule"],
                task["timezone"],
                after=(
                    utc_now()
                    - timedelta(seconds=1)
                ),
            )

            if next_run is None:
                if task["schedule_kind"] == "once":
                    raise AutomationStoreError(
                        "This one-time task's scheduled time has already passed. Edit the schedule first."
                    )

                raise AutomationStoreError(
                    "Could not calculate the next run."
                )

            next_run_at = utc_iso(
                next_run
            )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            enabled = ?,
            state = ?,
            next_run_at = ?,
            last_error = CASE
                WHEN ? = 1 THEN NULL
                ELSE last_error
            END,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            int(enabled),
            (
                "scheduled"
                if enabled
                else "paused"
            ),
            next_run_at,
            int(enabled),
            utc_iso(),
            int(task_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )


def request_manual_run(user_id, task_id):
    task = get_task(
        user_id,
        task_id,
    )

    if not task:
        raise AutomationStoreError(
            "Automation task was not found."
        )

    if task["state"] in {"running", "cancelling"}:
        raise AutomationStoreError(
            "This automation is already running or stopping."
        )

    if task["state"] == "needs_input":
        raise AutomationStoreError(
            "Edit this automation to resolve the requested clarification before running it again."
        )

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            manual_run_requested = 1,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            utc_iso(),
            int(task_id),
            int(user_id),
        ),
    )
    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )



def request_task_cancel(
    user_id,
    task_id,
    pause_after=False,
):
    """Request cooperative cancellation of the currently running task."""

    task = get_task(
        user_id,
        task_id,
    )

    if not task:
        raise AutomationStoreError(
            "Automation task was not found."
        )

    if task["state"] not in {
        "running",
        "cancelling",
    }:
        raise AutomationStoreError(
            "This automation is not currently running."
        )

    pause_after = bool(pause_after)
    timestamp = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            cancel_requested = 1,
            cancel_requested_at = COALESCE(
                cancel_requested_at,
                ?
            ),
            pause_after_cancel = CASE
                WHEN ? = 1 THEN 1
                ELSE pause_after_cancel
            END,
            enabled = CASE
                WHEN ? = 1 THEN 0
                ELSE enabled
            END,
            state = 'cancelling',
            updated_at = ?
        WHERE
            id = ?
            AND user_id = ?
            AND state IN ('running', 'cancelling')
        """,
        (
            timestamp,
            int(pause_after),
            int(pause_after),
            timestamp,
            int(task_id),
            int(user_id),
        ),
    )

    if cursor.rowcount != 1:
        conn.close()
        raise AutomationStoreError(
            "The running automation could not be stopped."
        )

    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )


def is_task_cancel_requested(
    user_id,
    task_id,
    lock_token=None,
):
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT cancel_requested
        FROM automation_tasks
        WHERE id = ? AND user_id = ?
    """
    params = [
        int(task_id),
        int(user_id),
    ]

    if lock_token:
        query += " AND lock_token = ?"
        params.append(str(lock_token))

    cursor.execute(
        query,
        params,
    )
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0])


def finish_task_cancelled(
    task,
    reason="Cancelled by user.",
    tool_log=None,
):
    """Finalize a user-requested cancellation without counting it as a failure."""

    task_id = int(task["id"])
    user_id = int(task["user_id"])
    run_id = int(task["run_id"])
    lock_token = str(
        task.get("lock_token")
        or ""
    )
    trigger_type = str(
        task.get("trigger_type")
        or "scheduler"
    )
    now_dt = utc_now()
    now_text = utc_iso(now_dt)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        _task_select_sql()
        + " WHERE id = ? AND user_id = ?",
        (
            task_id,
            user_id,
        ),
    )
    current = _task_from_row(
        cursor.fetchone()
    )

    if not current:
        cursor.execute(
            """
            UPDATE automation_runs
            SET
                finished_at = ?,
                status = 'cancelled',
                result = NULL,
                error = ?,
                condition_met = NULL,
                notified = 0,
                tool_log_json = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                now_text,
                str(reason)[:5000],
                json.dumps(
                    tool_log or [],
                    ensure_ascii=False,
                ),
                run_id,
                user_id,
            ),
        )
        conn.commit()
        conn.close()
        return None

    if (
        lock_token
        and current.get("lock_token")
        != lock_token
    ):
        conn.close()
        return current

    pause_after = bool(
        current.get("pause_after_cancel")
    )
    enabled = bool(current.get("enabled"))
    next_run_at = current.get(
        "next_run_at"
    )
    schedule_kind = current[
        "schedule_kind"
    ]

    if pause_after:
        enabled = False
        state = "paused"
    elif trigger_type == "scheduler":
        if schedule_kind == "once":
            enabled = False
            state = "cancelled"
            next_run_at = None
        elif enabled:
            next_run = compute_next_run(
                schedule_kind,
                current["schedule"],
                current["timezone"],
                after=now_dt,
            )
            state = "scheduled"
            next_run_at = (
                utc_iso(next_run)
                if next_run
                else None
            )
        else:
            state = "paused"
    else:
        previous_state = str(
            task.get("state")
            or "scheduled"
        )

        if previous_state in {
            "completed",
            "failed",
            "cancelled",
        }:
            state = previous_state
        elif enabled:
            state = "scheduled"

            if schedule_kind != "once":
                current_next = parse_utc(
                    next_run_at
                )
                if (
                    current_next is not None
                    and current_next <= now_dt
                ):
                    next_run = compute_next_run(
                        schedule_kind,
                        current["schedule"],
                        current["timezone"],
                        after=now_dt,
                    )
                    next_run_at = (
                        utc_iso(next_run)
                        if next_run
                        else None
                    )
        else:
            state = "paused"

    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            enabled = ?,
            state = ?,
            next_run_at = ?,
            last_run_at = ?,
            last_error = NULL,
            manual_run_requested = 0,
            locked_at = NULL,
            lock_token = NULL,
            cancel_requested = 0,
            cancel_requested_at = NULL,
            pause_after_cancel = 0,
            last_condition_met = NULL,
            last_notified = 0,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            int(enabled),
            state,
            next_run_at,
            now_text,
            now_text,
            task_id,
            user_id,
        ),
    )

    cursor.execute(
        """
        UPDATE automation_runs
        SET
            finished_at = ?,
            status = 'cancelled',
            result = NULL,
            error = ?,
            condition_met = NULL,
            notified = 0,
            tool_log_json = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            now_text,
            str(reason)[:5000],
            json.dumps(
                tool_log or [],
                ensure_ascii=False,
            ),
            run_id,
            user_id,
        ),
    )

    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )

def claim_next_task():
    initialize_automation_storage()
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "BEGIN IMMEDIATE"
        )

        now_text = utc_iso()
        cursor.execute(
            _task_select_sql()
            + """
            WHERE
                state NOT IN ('running', 'cancelling')
                AND EXISTS (
                    SELECT 1
                    FROM users AS u
                    WHERE
                        u.id = automation_tasks.user_id
                        AND u.status = 'active'
                )
                AND (
                    manual_run_requested = 1
                    OR (
                        enabled = 1
                        AND state = 'scheduled'
                        AND next_run_at IS NOT NULL
                        AND next_run_at <= ?
                    )
                )
            ORDER BY
                manual_run_requested DESC,
                COALESCE(next_run_at, '') ASC,
                id ASC
            LIMIT 1
            """,
            (now_text,),
        )

        row = cursor.fetchone()

        if not row:
            conn.commit()
            conn.close()
            return None

        task = _task_from_row(row)
        trigger_type = (
            "manual"
            if task["manual_run_requested"]
            else "scheduler"
        )
        scheduled_for = (
            now_text
            if trigger_type == "manual"
            else task["next_run_at"]
        )
        lock_token = secrets.token_hex(
            16
        )

        cursor.execute(
            """
            UPDATE automation_tasks
            SET
                state = 'running',
                manual_run_requested = 0,
                cancel_requested = 0,
                cancel_requested_at = NULL,
                pause_after_cancel = 0,
                locked_at = ?,
                lock_token = ?,
                updated_at = ?
            WHERE
                id = ?
                AND state NOT IN ('running', 'cancelling')
            """,
            (
                now_text,
                lock_token,
                now_text,
                task["id"],
            ),
        )

        if cursor.rowcount != 1:
            conn.rollback()
            conn.close()
            return None

        cursor.execute(
            """
            INSERT INTO automation_runs (
                task_id,
                user_id,
                trigger_type,
                scheduled_for,
                started_at,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                task["id"],
                task["user_id"],
                trigger_type,
                scheduled_for,
                now_text,
                now_text,
            ),
        )
        run_id = cursor.lastrowid

        conn.commit()
        conn.close()

        task["trigger_type"] = trigger_type
        task["scheduled_for"] = scheduled_for
        task["lock_token"] = lock_token
        task["run_id"] = run_id
        return task

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        raise


def finish_task_needs_input(
    task,
    clarification,
    notified=False,
    tool_log=None,
):
    """
    Pause an unattended task that genuinely requires user clarification.

    This is not counted as an execution failure. The schedule definition is
    preserved, but the task cannot run/resume until the user edits and saves it.
    """

    task_id = int(task["id"])
    user_id = int(task["user_id"])
    run_id = int(task["run_id"])
    clarification = str(
        clarification
        or "This automation needs more information before it can run unattended."
    ).strip()[:5000]
    now_text = utc_iso()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            enabled = 0,
            state = 'needs_input',
            next_run_at = NULL,
            last_run_at = ?,
            last_error = ?,
            preflight_status = 'needs_input',
            preflight_message = ?,
            preflight_updated_at = ?,
            consecutive_failures = 0,
            manual_run_requested = 0,
            locked_at = NULL,
            lock_token = NULL,
            cancel_requested = 0,
            cancel_requested_at = NULL,
            pause_after_cancel = 0,
            last_condition_met = NULL,
            last_notified = ?,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            now_text,
            clarification,
            clarification,
            now_text,
            int(bool(notified)),
            now_text,
            task_id,
            user_id,
        ),
    )

    cursor.execute(
        """
        UPDATE automation_runs
        SET
            finished_at = ?,
            status = 'needs_input',
            result = NULL,
            error = ?,
            condition_met = NULL,
            notified = ?,
            tool_log_json = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            now_text,
            clarification,
            int(bool(notified)),
            json.dumps(
                tool_log or [],
                ensure_ascii=False,
            ),
            run_id,
            user_id,
        ),
    )

    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )


def finish_task_run(
    task,
    success,
    result=None,
    error=None,
    condition_met=None,
    condition_key=None,
    notified=False,
    tool_log=None,
    max_failures=5,
):
    task_id = int(task["id"])
    user_id = int(task["user_id"])
    run_id = int(task["run_id"])
    lock_token = str(
        task.get("lock_token")
        or ""
    )
    trigger_type = str(
        task.get("trigger_type")
        or "scheduler"
    )
    now_dt = utc_now()
    now_text = utc_iso(now_dt)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        _task_select_sql()
        + " WHERE id = ? AND user_id = ?",
        (
            task_id,
            user_id,
        ),
    )
    current = _task_from_row(
        cursor.fetchone()
    )

    if not current:
        cursor.execute(
            """
            UPDATE automation_runs
            SET
                finished_at = ?,
                status = ?,
                result = ?,
                error = ?,
                condition_met = ?,
                notified = ?,
                tool_log_json = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                now_text,
                "success" if success else "failed",
                result,
                error,
                (
                    None
                    if condition_met is None
                    else int(bool(condition_met))
                ),
                int(bool(notified)),
                json.dumps(
                    tool_log or [],
                    ensure_ascii=False,
                ),
                run_id,
                user_id,
            ),
        )
        conn.commit()
        conn.close()
        return None

    if (
        lock_token
        and current.get("lock_token")
        != lock_token
    ):
        conn.close()
        return current

    failures = int(
        current.get(
            "consecutive_failures"
        )
        or 0
    )

    if trigger_type == "scheduler":
        if success:
            failures = 0
        else:
            failures += 1

    schedule_kind = current[
        "schedule_kind"
    ]
    enabled = bool(current["enabled"])
    next_run_at = current[
        "next_run_at"
    ]

    if trigger_type == "scheduler":
        if schedule_kind == "once":
            enabled = False
            state = (
                "completed"
                if success
                else "failed"
            )
            next_run_at = None
        else:
            if (
                not success
                and failures >= max_failures
            ):
                enabled = False
                state = "failed"
                next_run_at = None
            elif enabled:
                next_run = compute_next_run(
                    schedule_kind,
                    current["schedule"],
                    current["timezone"],
                    after=now_dt,
                )
                state = "scheduled"
                next_run_at = (
                    utc_iso(next_run)
                    if next_run
                    else None
                )
            else:
                state = "paused"
    else:
        # A manual run does not mutate the user's recurring schedule. Restore
        # the state that existed when the task was claimed.
        previous_state = str(
            task.get("state")
            or "scheduled"
        )

        if previous_state in {
            "completed",
            "failed",
        }:
            state = previous_state
        elif current["enabled"]:
            state = "scheduled"

            # If a recurring schedule became due while a manual run was in
            # progress, advance it once instead of immediately running the
            # same task again when the manual execution finishes.
            if schedule_kind != "once":
                current_next = parse_utc(
                    next_run_at
                )
                if (
                    current_next is not None
                    and current_next <= now_dt
                ):
                    next_run = compute_next_run(
                        schedule_kind,
                        current["schedule"],
                        current["timezone"],
                        after=now_dt,
                    )
                    next_run_at = (
                        utc_iso(next_run)
                        if next_run
                        else None
                    )
        else:
            state = "paused"

    if (
        condition_met is False
        and current["task_type"]
        == "condition"
    ):
        condition_key_to_store = None
    elif condition_key is not None:
        condition_key_to_store = str(
            condition_key
        )[:500]
    else:
        condition_key_to_store = current.get(
            "last_condition_key"
        )

    cursor.execute(
        """
        UPDATE automation_tasks
        SET
            enabled = ?,
            state = ?,
            next_run_at = ?,
            last_run_at = ?,
            last_result = ?,
            last_error = ?,
            last_condition_key = ?,
            consecutive_failures = ?,
            locked_at = NULL,
            lock_token = NULL,
            cancel_requested = 0,
            cancel_requested_at = NULL,
            pause_after_cancel = 0,
            last_condition_met = ?,
            last_notified = ?,
            updated_at = ?
        WHERE
            id = ?
            AND user_id = ?
        """,
        (
            int(enabled),
            state,
            next_run_at,
            now_text,
            result,
            (
                None
                if success
                else str(error or "Automation failed.")[:5000]
            ),
            condition_key_to_store,
            failures,
            (
                None
                if condition_met is None
                else int(bool(condition_met))
            ),
            int(bool(notified)),
            now_text,
            task_id,
            user_id,
        ),
    )

    cursor.execute(
        """
        UPDATE automation_runs
        SET
            finished_at = ?,
            status = ?,
            result = ?,
            error = ?,
            condition_met = ?,
            notified = ?,
            tool_log_json = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            now_text,
            "success" if success else "failed",
            result,
            (
                None
                if success
                else str(error or "Automation failed.")[:5000]
            ),
            (
                None
                if condition_met is None
                else int(bool(condition_met))
            ),
            int(bool(notified)),
            json.dumps(
                tool_log or [],
                ensure_ascii=False,
            ),
            run_id,
            user_id,
        ),
    )

    conn.commit()
    conn.close()
    return get_task(
        user_id,
        task_id,
    )


def list_runs(
    user_id,
    task_id=None,
    limit=50,
):
    initialize_automation_storage()
    limit = max(
        1,
        min(200, int(limit)),
    )

    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT
            r.id,
            r.task_id,
            r.user_id,
            r.trigger_type,
            r.scheduled_for,
            r.started_at,
            r.finished_at,
            r.status,
            r.result,
            r.error,
            r.condition_met,
            r.notified,
            r.tool_log_json,
            r.created_at,
            t.title
        FROM automation_runs AS r
        LEFT JOIN automation_tasks AS t
            ON t.id = r.task_id
        WHERE r.user_id = ?
    """
    params = [int(user_id)]

    if task_id is not None:
        query += " AND r.task_id = ?"
        params.append(int(task_id))

    query += " ORDER BY r.id DESC LIMIT ?"
    params.append(limit)

    cursor.execute(
        query,
        tuple(params),
    )
    rows = cursor.fetchall()
    conn.close()

    runs = []

    for row in rows:
        try:
            tool_log = json.loads(
                row[12] or "[]"
            )
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            tool_log = []

        runs.append({
            "id": row[0],
            "task_id": row[1],
            "user_id": row[2],
            "trigger_type": row[3],
            "scheduled_for": row[4],
            "started_at": row[5],
            "finished_at": row[6],
            "status": row[7],
            "result": row[8],
            "error": row[9],
            "condition_met": (
                None
                if row[10] is None
                else bool(row[10])
            ),
            "notified": bool(row[11]),
            "tool_log": tool_log,
            "created_at": row[13],
            "task_title": (
                row[14]
                or "Deleted task"
            ),
        })

    return runs
