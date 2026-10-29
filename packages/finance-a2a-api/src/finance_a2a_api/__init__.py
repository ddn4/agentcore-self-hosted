"""The Finance agent's published A2A-shaped contract.

This is the ONLY code shared between team Support and team Finance. It stays
dependency-free and is type-checked with pyright strict. The Agent Card,
Message/Part, Task/TaskState and Artifact types arrive in v0.4.
"""

from typing import Final

#: Nexus endpoint name in the Temporal endpoint registry (ADR-0004).
ENDPOINT: Final[str] = "finance-agent"
#: Nexus service name: `get_agent_card` (sync) and `send_message` (workflow-backed).
SERVICE: Final[str] = "FinanceAgentA2A"

__all__ = ["ENDPOINT", "SERVICE"]
