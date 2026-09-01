"""
Runtime bridge for v2.1.1.

Explicit feedback affects the current revision immediately through agent_inputs.
Only after the revised run completes do we optionally distill DURABLE feedback
into the selected Agent's long-term memory.

This establishes a trust hierarchy:
explicit user correction > reflection/speculation.

The evaluator is conservative: task-specific requests remain in revision
history but do not become permanent Agent Memory.
"""

import json

from app.config import DEFAULT_MODEL
from app.ollama_client import chat_once
from app.services import agent_runner
from app.services.agent_identity import (
    add_agent_memory,
    get_run_agent_profile,
)
from app.services.agent_revision import (
    complete_latest_revision,
    get_feedback_event,
    update_feedback_learning,
)


_APPLIED = False


def _learn_from_feedback(
    run,
    revision,
):
    if not revision:
        return

    event = get_feedback_event(
        run[
            "user_id"
        ],
        revision[
            "feedback_event_id"
        ],
    )

    if (
        not event
        or not event[
            "learn_opt_in"
        ]
        or event[
            "learning_status"
        ] != "pending"
    ):
        return

    profile = get_run_agent_profile(
        run[
            "user_id"
        ],
        run[
            "id"
        ],
    )

    if (
        not profile
        or not profile[
            "memory_enabled"
        ]
    ):
        update_feedback_learning(
            run[
                "user_id"
            ],
            event[
                "id"
            ],
            status=
                "skipped_memory_disabled",
        )
        return

    system_prompt = """
You evaluate explicit USER FEEDBACK given to a persistent ATLAS Agent.

This is a higher-trust learning signal than autonomous reflection, but only
DURABLE reusable feedback belongs in Agent Memory.

Store a memory only when the user's feedback expresses a reusable rule,
preference, procedure, correction pattern, or durable project convention.

Examples worth remembering:
- Do not remove a requested dependency just to satisfy the sandbox.
- After repairing code from a failed test, re-run tests before more edits.
- For research comparisons, verify important specs across independent sources.
- The user prefers a certain durable workflow for this Agent.

Do NOT memorize:
- "make this button blue" for one artifact
- a one-time deadline or current price
- secrets/passwords/tokens/account numbers
- transient search facts
- unsupported model speculation
- permission/security changes
- instructions to modify ATLAS security or grant tools

Generalize the lesson enough to be reusable, but preserve the user's intent.
Maximum 2 memories. Zero is valid.

Return ONLY JSON:
{
  "memories": [
    {
      "content": "durable reusable lesson",
      "category": "procedure|lesson|preference|domain|project_pattern|general",
      "importance": 1-10
    }
  ]
}
"""

    user_prompt = (
        "AGENT:\n"
        + profile[
            "name"
        ]
        + "\n\nORIGINAL GOAL:\n"
        + str(
            run.get(
                "goal"
            )
            or ""
        )
        + "\n\nEXPLICIT USER FEEDBACK:\n"
        + event[
            "content"
        ]
        + "\n\nREVISED FINAL RESULT:\n"
        + str(
            run.get(
                "result"
            )
            or ""
        )[
            :5000
        ]
    )

    memory_ids = []

    try:
        response = chat_once(
            model=
                DEFAULT_MODEL,
            messages=[
                {
                    "role":
                        "system",
                    "content":
                        system_prompt,
                },
                {
                    "role":
                        "user",
                    "content":
                        user_prompt,
                },
            ],
            response_format=
                "json",
            options={
                "temperature":
                    0,
            },
            timeout=
                300,
        )

        content = (
            response
            .get(
                "message",
                {},
            )
            .get(
                "content",
                "",
            )
            .strip()
        )

        parsed = json.loads(
            content
        )

        candidates = (
            parsed.get(
                "memories"
            )
            or []
        )

        if not isinstance(
            candidates,
            list,
        ):
            candidates = []

        for candidate in candidates[
            :2
        ]:
            if not isinstance(
                candidate,
                dict,
            ):
                continue

            text = str(
                candidate.get(
                    "content"
                )
                or ""
            ).strip()

            if not text:
                continue

            memory = add_agent_memory(
                run[
                    "user_id"
                ],
                profile[
                    "id"
                ],
                text,
                category=
                    candidate.get(
                        "category",
                        "general",
                    ),
                importance=
                    candidate.get(
                        "importance",
                        8,
                    ),
                confidence=
                    0.99,
                source=
                    "user_feedback",
                source_run_id=
                    run[
                        "id"
                    ],
            )

            if not memory.get(
                "duplicate"
            ):
                memory_ids.append(
                    int(
                        memory[
                            "id"
                        ]
                    )
                )

        update_feedback_learning(
            run[
                "user_id"
            ],
            event[
                "id"
            ],
            status=
                (
                    "stored"
                    if memory_ids
                    else "no_durable_memory"
                ),
            memory_ids=
                memory_ids,
        )

    except Exception:
        # Feedback remains permanently stored as run/revision provenance even
        # when optional learning fails.
        update_feedback_learning(
            run[
                "user_id"
            ],
            event[
                "id"
            ],
            status=
                "learning_error",
        )


def apply_agent_revision_runtime():
    global _APPLIED

    if _APPLIED:
        return

    original_finish = (
        agent_runner
        ._finish_with_final
    )

    def finish_with_revision(
        run,
        data,
    ):
        answer = original_finish(
            run,
            data,
        )

        # The original finalizer has already persisted result/state.
        refreshed = dict(
            run
        )

        try:
            from app.services.agents import (
                get_agent_run,
            )

            current = get_agent_run(
                run[
                    "user_id"
                ],
                run[
                    "id"
                ],
            )

            if current:
                refreshed = current
        except Exception:
            pass

        revision = None

        try:
            revision = complete_latest_revision(
                refreshed[
                    "user_id"
                ],
                refreshed[
                    "id"
                ],
                refreshed.get(
                    "result"
                )
                or answer,
            )
        except Exception:
            revision = None

        if revision:
            try:
                _learn_from_feedback(
                    refreshed,
                    revision,
                )
            except Exception:
                pass

        return answer

    agent_runner._finish_with_final = (
        finish_with_revision
    )

    _APPLIED = True
