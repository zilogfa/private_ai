"""
v2.0.2 agent budget profile.

Keep the hard ceiling finite. Bigger projects get more room, but the agent
still has explicit bounds while adaptive self-extension is designed later.
"""

import app.config as config


AGENT_BUDGETS = {
    "quick": 6,
    "standard": 12,
    "deep": 25,
    "project": 40,
}

AGENT_RUNTIME_SECONDS = 3600


def apply_agent_budget_upgrade():
    config.AGENT_DEFAULT_MAX_STEPS = AGENT_BUDGETS["standard"]
    config.AGENT_MAX_STEPS = AGENT_BUDGETS["project"]
    config.AGENT_MAX_RUNTIME_SECONDS = max(
        int(
            getattr(
                config,
                "AGENT_MAX_RUNTIME_SECONDS",
                1200,
            )
        ),
        AGENT_RUNTIME_SECONDS,
    )

    # agent_runner may already be imported in some future startup order.
    # Patch its copied runtime constant if it exists.
    try:
        from app.services import agent_runner

        agent_runner.AGENT_MAX_RUNTIME_SECONDS = (
            config.AGENT_MAX_RUNTIME_SECONDS
        )
    except Exception:
        pass

    return {
        "budgets": dict(AGENT_BUDGETS),
        "max_steps": config.AGENT_MAX_STEPS,
        "runtime_seconds": config.AGENT_MAX_RUNTIME_SECONDS,
    }
