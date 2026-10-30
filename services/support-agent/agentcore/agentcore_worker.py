"""AgentCore Runtime entrypoint for the Support Worker (``entrypoint`` in agentcore.json).

``make agentcore-bundle`` copies this file to the root of ``build/agentcore/`` next to
``support_agent/`` and the vendored ``finance_a2a_api/``, so both import from the code
location without a workspace install. The Worker itself is
``support_agent.agentcore_app``. samples-python ``bedrock_agentcore/strands_agent`` puts
the whole app in its ``agentcore_worker.py``; this file is deliberately thin instead, so
the host (guard, async task, drain) lives in the package where unit tests drive it.
"""

from support_agent.agentcore_app import app, main

__all__ = ["app"]

if __name__ == "__main__":
    main()
