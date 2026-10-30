# ADR-0005: AgentCore order: the PUBLIC runtime is a v0.8 probe only

- Status: **Accepted (v0.8); exercised live** on 2026-10-30 (resumed run): deployed, invoked
  by the dev server's WCI, torn down the same day (story v0.8).
- Runbook: 1.2, 5.4 (`networkMode`), 6.x; plan §1 "Why AgentCore VPC mode (v0.12) comes
  after EKS, and why a cheap probe (v0.8) can come before it", v0.8, §3.6.

## Context

The demo's AgentCore Worker must reach a **private** Temporal frontend (internal NLB, mTLS),
so the real runtime is VPC mode, which needs the v0.10 network and the v0.11 server. Most
AgentCore risk is not about networking, though: IAM (execution role, invocation role, External
ID), the self-hosted CFN template, the AgentCore CLI 1.0.x, CodeZip packaging, and the WCI's
AssumeRole → `GetAgentRuntimeEndpoint` → `InvokeAgentRuntime` handshake. All of that can be
proven for cents with a PUBLIC runtime and the laptop dev server, whose WCI v0.7 showed exists.

## Decision

1. **A PUBLIC runtime exists only as the v0.8 probe** and is never the demo path. It is
   created and deleted the same day (`make agentcore-deploy MODE=PUBLIC` …
   `make agentcore-down`).
2. Its Worker is **meant to fail**: `TEMPORAL_ADDRESS=127.0.0.1:7233` is unreachable inside
   AgentCore, `TEMPORAL_CONNECT_TIMEOUT_SECONDS=10` bounds the attempt, and
   `complete_async_task` in `finally` releases the session. No mTLS secret
   (`TEMPORAL_MTLS_SECRET_ID` empty, so `support_agent.mtls` is a no-op), TLS off.
3. Short idle timeout for the probe: `idleRuntimeSessionTimeout` **120 s** (CLI minimum 60),
   so sessions the WCI opens end quickly. The demo runtime keeps 900 s.
4. A **local versioned Worker** of the same Build ID (`make worker-support-versioned`) makes
   registration succeed while the AgentCore Worker cannot connect (plan v0.8 step 6).
5. Same names as the real runtime, so v0.12 changes only the network: CLI project
   `AgentcoreDemo`, runtime `AgentcoreDemo_default_support_supervisor` (the CLI adds the target), endpoint `b_<build7>`,
   execution role `agentcore-demo-support-exec` (Terraform, `executionRoleArn`), tags
   `Project=agentcore-self-hosted`.
6. VPC mode comes at v0.12, after EKS (v0.10/v0.11), as the plan orders it.

## Consequences

- Q13 (`--allow-no-pollers`) is **not answered** by v0.8: a local Worker polls during
  registration. Definitive at v0.12.
- Q15 from v0.8 is a first look that can only be "yes", "no" or "inconclusive"; the
  definitive answer is v0.13 (real session start latency and idle timer, in VPC mode).
- The probe's invocations are expected to "fail" on the Worker side; what is measured is
  the WCI's side (history, cadence, session count back to 0).

## Outcome (v0.8, 2026-10-30)

- As designed: 14 sessions, each "AgentCore app" → connect to `127.0.0.1:7233` **refused**
  in < 0.5 s (so the 10 s bound was not reached) → `async task released`; the 120 s idle
  timeout ended them; sessions of ours 0 after teardown.
- Registration with the local Worker polling succeeded (`update-version-compute-config`,
  then `set-current-version` without `--allow-no-pollers`): Q13 stays unanswered, as decided.
- Q15 first look: **yes** (invoke 4 ms after `NexusOperationCompleted`).
- Surprise worth carrying to v0.13: a Worker that cannot connect leaves the backlog in
  place, and the WCI re-invokes every ~60–68 s (after ~5 min); after the Workflow was
  terminated it still invoked 5 times in 4.5 min. In VPC mode a misconfigured network would
  burn sessions at that rate: the session alarm and `agentcore-sessions.sh` are the guard.
