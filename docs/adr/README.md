# Architecture Decision Records

| ADR | Title | Status | Tag |
|---|---|---|---|
| [0001](0001-model-pinning-and-effort.md) | Model pinning and effort; structured output via capture tool | Accepted; amended v0.2 (live confirmation pending) | v0.1, v0.2 |
| [0002](0002-hitl-enforced-in-code.md) | HITL enforced in code (`input-required`, `approve`/`reject` signals, approval timeout), not Strands `interrupt()` | Accepted | v0.6 |
| [0003](0003-agents-as-tools-and-a2a-shape.md) | Nested agents as tools (fresh `TemporalAgent` per in-workflow `@tool`, not `Agent.as_tool()`); A2A as a tool vs handoff | Part 1 accepted (v0.3); part 2 accepted (v0.5: tool default, handoff kept as the fallback) | v0.3/v0.5 |
| [0004](0004-naming-build-id-nexus-backing.md) | Naming, content-hash Build ID, Nexus backing (workflow-backed, `USE_EXISTING`; Activity backing descoped) | Accepted; implemented v0.4 (backing) and v0.7 (Build ID) | v0.1, v0.4, v0.7 |
| [0005](0005-public-runtime-is-a-probe.md) | AgentCore order: the PUBLIC runtime is a v0.8 probe only (fails fast to 127.0.0.1, 120 s idle, same-day teardown; VPC at v0.12) | Accepted (v0.8; exercised live and torn down the same day) | v0.8 |
| [0006](0006-demo-sizing.md) | Demo sizing deviations from the runbook: 2 × m7g.large (fallback m6i.xlarge), RDS db.t4g.medium single-AZ, PostgreSQL 16.15 (16.6 no longer offered), endpoints in 2 AZs, single NAT, no CMK, 512 shards, 30 s cloud debounce | Accepted by the user; written and applied at v0.10; v0.11 section (what runs on the nodes) | v0.10, v0.11 |
| [0007](0007-authorization-option-c.md) | Authorization option (c) as an accepted risk; External ID and CA out of Terraform state | Accepted; v0.9 section: exercised on kind (one CA, distinct CNs, negatives, NetworkPolicy, chart mounts the frontend key into admintools); v0.11 section: EKS CA, NLB passthrough, SG + NetworkPolicy layers | v0.1, v0.9, v0.11 |
| [0008](0008-agentcore-runtime-via-cli.md) | AgentCore runtime via the CLI 1.0.1 (existing Terraform execution role, no IAM in the CLI stack); `aws_bedrockagentcore_agent_runtime` is the fallback | Accepted (v0.8); revisit at v0.12 | v0.8/v0.12 |
| [0009](0009-nexus-timeout-budget.md) | Nexus timeout budget (caller 25 h > Finance's published 24 h); how the values reach Workflow code | Accepted | v0.6 |
