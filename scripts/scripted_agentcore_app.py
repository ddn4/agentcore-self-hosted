"""Offline stand-in for ``make agentcore-local``: the same AgentCore app, scripted model.

    uv run python scripts/scripted_agentcore_app.py [--delay 0.3]   (make agentcore-local-scripted)

Builds the entrypoint with ``support_agent.agentcore_app.create_app`` exactly like the
real one (single-start guard, async task, PINNED versioned Worker on ``support-tq``,
debounce, drain, ``complete_async_task``), but each Worker lifetime's StrandsPlugin binds
``"bedrock"`` to a ``demo_testing.ScriptedModel`` that plays the Support team (as
``scripts/scripted_worker.py`` does for the unversioned Worker). Lets the verify script
run Case B through two AgentCore-shaped Worker lifetimes without AWS. Test seam only.
"""

from __future__ import annotations

import argparse
import functools

from demo_testing.scripted_model import ScriptedModel, scripted_plugin
from demo_testing.support_script import SupportPrompts, support_responder
from support_agent import agentcore_app, agents

PROMPTS = SupportPrompts(
    supervisor=agents.SUPERVISOR_PROMPT,
    order_lookup=agents.ORDER_LOOKUP_PROMPT,
    return_policy=agents.RETURN_POLICY_PROMPT,
    supervisor_handoff=agents.SUPERVISOR_HANDOFF_PROMPT,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentCore-shaped Support app, scripted model")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds per model turn")
    args = parser.parse_args()
    model = ScriptedModel(support_responder(PROMPTS), delay=args.delay)
    run = functools.partial(agentcore_app.run_worker, plugins=lambda: [scripted_plugin(model)])
    app, _host = agentcore_app.create_app(run)
    agentcore_app.main(app)


if __name__ == "__main__":
    main()
