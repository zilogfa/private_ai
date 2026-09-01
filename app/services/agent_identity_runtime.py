"""
Runtime hooks for ATLAS v2.1 Agent Identity + Agent Memory.

The hooks are intentionally narrow:
- link new runs to one user-owned Agent identity
- add that identity and its relevant memories to LOCAL model prompts
- never add private identity/memory context to the PUBLIC-ONLY web query planner
- run a conservative post-run reflection that can only create agent memories

Reflection cannot rewrite profile instructions, permissions, ATLAS core code, or
tool capabilities.
"""

import json

from flask import (
    has_request_context,
    request,
)

from app.config import (
    DEFAULT_MODEL,
)
from app.ollama_client import (
    chat_once,
)
from app.services import (
    agent_runner,
    agents,
)
from app.services.agent_identity import (
    AgentIdentityError,
    add_agent_memory,
    agent_context_for_run,
    ensure_default_agent_profile,
    get_agent_profile,
    get_run_agent_profile,
    link_run_to_agent_profile,
    reflection_exists,
    save_reflection_record,
)
from app.services.agents import (
    get_agent_run,
    list_agent_steps,
)


_APPLIED = False


def _public_only_prompt(
    system_prompt,
):
    text = str(
        system_prompt
        or ""
    ).lower()

    return (
        "public-only research subplanner"
        in text
        or (
            "you never see private memory"
            in text
            and "search query"
            in text
        )
    )


def _requested_profile_id(
    payload,
):
    payload = dict(
        payload
        or {}
    )

    value = str(
        payload.get(
            "agent_profile_id"
        )
        or ""
    ).strip()

    if value:
        return value

    if has_request_context():
        value = str(
            request.cookies.get(
                "atlas_agent_identity"
            )
            or ""
        ).strip()

    return (
        value
        or None
    )


def _reflection_ledger(
    run,
):
    steps = list_agent_steps(
        run[
            "user_id"
        ],
        run[
            "id"
        ],
    )

    blocks = []

    for step in steps[
        -30:
    ]:
        blocks.append(
            (
                f"Step {step.get('step_index')} "
                f"{step.get('action') or step.get('phase')} "
                f"[{step.get('status')}]\n"
                f"Reason: {step.get('reason') or ''}\n"
                f"Observation: {str(step.get('output') or '')[:1800]}"
            )
        )

    return "\n\n".join(
        blocks
    )[-18000:]


def _reflect_on_completed_run(
    run,
):
    if not run:
        return

    current = get_agent_run(
        run[
            "user_id"
        ],
        run[
            "id"
        ],
    )

    if (
        not current
        or current.get(
            "state"
        )
        != "completed"
        or reflection_exists(
            current[
                "id"
            ]
        )
    ):
        return

    profile = get_run_agent_profile(
        current[
            "user_id"
        ],
        current[
            "id"
        ],
    )

    if (
        not profile
        or not profile[
            "reflection_enabled"
        ]
        or not profile[
            "memory_enabled"
        ]
    ):
        return

    system_prompt = """
You are the private post-run reflection engine for one persistent ATLAS Agent.

You are NOT answering the user.
You are deciding whether this Agent learned durable, reusable working knowledge.

Good Agent Memory:
- a reusable procedure that worked
- a lesson learned from a failure or correction
- a stable project convention
- a durable collaboration/workflow preference explicitly revealed in this run
- domain knowledge that is stable enough to help future runs

Do NOT store:
- passwords, API keys, tokens, account numbers, security codes
- unrelated private personal facts about the user
- transient emotions
- one-off search results, current prices, rapidly changing news
- exact final-answer prose merely because it was generated
- unsupported guesses
- permissions or security-policy changes
- instructions to rewrite ATLAS core code
- a claim that tests passed unless the run actually observed success

The current Agent profile/instructions cannot be modified by reflection.
Reflection can only propose memories.

Be conservative. Zero memories is perfectly valid.
Normally propose no more than 3.

Allowed categories:
procedure, lesson, preference, domain, project_pattern, general

Return ONLY JSON:
{
  "summary": "one short sentence about what the agent learned, or empty",
  "memories": [
    {
      "content": "durable reusable memory",
      "category": "procedure",
      "importance": 7,
      "confidence": 0.92
    }
  ]
}
"""

    user_prompt = (
        "AGENT:\n"
        + profile[
            "name"
        ]
        + "\n\nAGENT DESCRIPTION:\n"
        + (
            profile[
                "description"
            ]
            or "None"
        )
        + "\n\nGOAL:\n"
        + str(
            current.get(
                "goal"
            )
            or ""
        )
        + "\n\nFINAL RESULT:\n"
        + str(
            current.get(
                "result"
            )
            or ""
        )[
            :6000
        ]
        + "\n\nRUN LEDGER:\n"
        + (
            _reflection_ledger(
                current
            )
            or "No recorded steps."
        )
    )

    proposed = []
    summary = ""

    try:
        data = chat_once(
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
            data
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

        summary = str(
            parsed.get(
                "summary"
            )
            or ""
        ).strip()[
            :4000
        ]

        candidate_memories = (
            parsed.get(
                "memories"
            )
            or []
        )

        if isinstance(
            candidate_memories,
            list,
        ):
            proposed = candidate_memories[
                :3
            ]

    except Exception:
        # Reflection is optional learning. It must never turn a successfully
        # completed user run into a failure.
        proposed = []
        summary = ""

    stored_count = 0

    for item in proposed:
        if not isinstance(
            item,
            dict,
        ):
            continue

        content = str(
            item.get(
                "content"
            )
            or ""
        ).strip()

        if not content:
            continue

        try:
            confidence = float(
                item.get(
                    "confidence",
                    0.0,
                )
            )
        except (
            TypeError,
            ValueError,
        ):
            confidence = 0.0

        if confidence < 0.70:
            continue

        try:
            memory = add_agent_memory(
                current[
                    "user_id"
                ],
                profile[
                    "id"
                ],
                content,
                category=
                    item.get(
                        "category",
                        "general",
                    ),
                importance=
                    item.get(
                        "importance",
                        5,
                    ),
                confidence=
                    confidence,
                source=
                    "reflection",
                source_run_id=
                    current[
                        "id"
                    ],
            )

            if not memory.get(
                "duplicate"
            ):
                stored_count += 1

        except Exception:
            continue

    try:
        save_reflection_record(
            current,
            profile,
            summary,
            len(
                proposed
            ),
            stored_count,
        )
    except Exception:
        pass


def apply_agent_identity_runtime():
    global _APPLIED

    if _APPLIED:
        return

    ensure = (
        ensure_default_agent_profile
    )

    # -----------------------------------------------------
    # New-run identity linkage
    # -----------------------------------------------------

    original_create_agent_run = (
        agents.create_agent_run
    )

    def create_agent_run_with_identity(
        user_id,
        payload,
    ):
        ensure(
            user_id
        )

        run = original_create_agent_run(
            user_id,
            payload,
        )

        try:
            link_run_to_agent_profile(
                user_id,
                run[
                    "id"
                ],
                _requested_profile_id(
                    payload
                ),
            )
        except AgentIdentityError:
            link_run_to_agent_profile(
                user_id,
                run[
                    "id"
                ],
                None,
            )

        return run

    agents.create_agent_run = (
        create_agent_run_with_identity
    )

    # -----------------------------------------------------
    # Local identity + memory context
    # -----------------------------------------------------

    original_run_model = (
        agent_runner._run_model
    )

    def run_model_with_agent_identity(
        run,
        system_prompt,
        user_prompt,
        *args,
        **kwargs,
    ):
        if not _public_only_prompt(
            system_prompt
        ):
            context = agent_context_for_run(
                run
            )

            if context:
                system_prompt = (
                    context
                    + "\n\n"
                    + str(
                        system_prompt
                        or ""
                    )
                )

        return original_run_model(
            run,
            system_prompt,
            user_prompt,
            *args,
            **kwargs,
        )

    agent_runner._run_model = (
        run_model_with_agent_identity
    )

    # -----------------------------------------------------
    # Conservative post-run learning
    # -----------------------------------------------------

    original_finish = (
        agent_runner._finish_with_final
    )

    def finish_with_agent_reflection(
        run,
        data,
    ):
        answer = original_finish(
            run,
            data,
        )

        try:
            _reflect_on_completed_run(
                run
            )
        except Exception:
            pass

        return answer

    agent_runner._finish_with_final = (
        finish_with_agent_reflection
    )

    _APPLIED = True
