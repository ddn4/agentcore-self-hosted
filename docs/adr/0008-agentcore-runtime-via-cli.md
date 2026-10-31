# ADR-0008: AgentCore runtime via the AgentCore CLI, not Terraform

- Status: **Accepted (v0.8)**; revisited at v0.12 (VPC mode).
- Runbook: 5.4, 5.5, Phase 11; AWS open question Q6; plan §2.2 (`30-agentcore-iam`: "NO CFN
  stack, NO External ID"), v0.8, v0.12; ledger 27, 45.

## Context

Three things touch AgentCore: IAM (execution role, invocation role), the runtime with its
per-Build-ID endpoints, and the code artifact. AWS Q6 asked whether Terraform can manage the
runtime at all. v0.8 answered it from the provider schema (`terraform providers schema -json`,
hashicorp/aws **6.71.0**): `aws_bedrockagentcore_agent_runtime` and
`aws_bedrockagentcore_agent_runtime_endpoint` exist (plus gateway, memory, workload identity
and 17 more `aws_bedrockagentcore_*` resources). So Terraform *could* own it.

## Decision

1. **The runtime and its endpoints are deployed with the AgentCore CLI** (`@aws/agentcore`
   **1.0.1**, pinned in `make tools-agentcore`), through `scripts/agentcore-deploy.sh`:
   - the CLI packages CodeZip for AgentCore's platform itself
     (`uv pip install -r pyproject.toml --target … --python-platform aarch64-manylinux2014
     --only-binary :all:`, from `@aws/agentcore-cdk` 1.0.0), which Terraform would need a
     separate build step for;
   - endpoints are one per Build ID (`b_<build7>`, ADR-0004) and change with releases, not
     with infrastructure; release tooling (`register-version`) sits next to them;
   - it is the path the Temporal and AWS docs and the sample use.
2. **IAM stays in Terraform** (`30-agentcore-iam`), and the CLI is given the existing role
   (`executionRoleArn` in `agentcore.json`): the CLI stack then contains **no IAM resource**
   (only `AWS::BedrockAgentCore::Runtime` and `::RuntimeEndpoint`; checked in verify step 8).
3. **The invocation role is neither**: a CFN stack from Temporal's vendored template, managed
   only by `scripts/invoke-role.sh` (External ID kept out of Terraform state, ADR-0007).
4. **Fallback:** `aws_bedrockagentcore_agent_runtime` + `_endpoint` in Terraform, with a
   CodeZip artifact built by `make agentcore-bundle` plus the same `uv pip install` step.

## What the CLI 1.0.1 does that we had to account for (v0.8)

- Projects are v1 format: `agentcore.json` `"version": 2` plus an `agentcore/cdk/` app.
  A `"version": 1` file runs in "v0 compatibility mode" and refuses `agentcore build`.
  `scripts/agentcore-deploy.sh project` scaffolds the project with
  `agentcore create --template empty` and copies the CodeZip bundle into
  `app/support_supervisor/` (`codeLocation`).
- `agentcore build` is a CDK synth (no `validate` command). Unknown keys (`$schema`) are
  rejected.
- The entrypoint is wrapped in `opentelemetry-instrument` unless
  `"instrumentation": {"enableOtel": false}`; our dependency set has no OTel distro, so it is
  off.
- `agentcore deploy` turns on CloudWatch Transaction Search account-wide unless
  `agentcore config transactionSearch false`; `scripts/agentcore.sh` sets that (and telemetry
  off) before every call. The CLI keeps that in `~/.agentcore/config.json`.
- Deploy target: CFN stack `AgentCore-AgentcoreDemo-default`, CDK bootstrap `CDKToolkit`
  (present in the account).
- **No `destroy` command** (Phase 11 / ledger 45): `agentcore remove all --yes` then
  `agentcore deploy --yes` removes the stack; `scripts/agentcore-deploy.sh down` does that and
  falls back to `aws cloudformation delete-stack` on that one stack name.
- Commands present in 1.0.1: `create add dev deploy invoke status logs traces export migrate
  remove build`, resource groups `runtime (get list invoke shell exec version endpoint logs
  traces)`, `harness gateway identity memory eval payment`, settings `config update`.

## Live deploy and teardown (v0.8 resume, 2026-10-30)

- `make agentcore-deploy MODE=PUBLIC` deployed in a few minutes (one CFN stack, no IAM);
  `agentcore status --target default --json` reports the runtime and the endpoint
  `b_fd7af7a` as `deployed`; the endpoint is `READY` on runtime version 1.
- **The runtime name includes the target**: `<project>_<target>_<runtime>` =
  `AgentcoreDemo_default_support_supervisor` (not `AgentcoreDemo_support_supervisor` as read
  from the synth). `scripts/agentcore-deploy.sh` now looks it up by that name
  (`AGENTCORE_TARGET`, default `default`). The `AgentcoreDemo_` prefix used by the execution
  role's log/workload statements and by `agentcore-sessions.sh` still matches. Log groups:
  `/aws/bedrock-agentcore/runtimes/<runtime id>-<endpoint>` (also `-DEFAULT`).
- Synth before the first deploy names the stack `AgentCore-AgentcoreDemo`; once
  `aws-targets.json` exists, `AgentCore-AgentcoreDemo-default`.
- **Tags**: the `tags` of `agentcore.json` (`Project=agentcore-self-hosted`) reach the
  runtime, **not** the CloudFormation stack, which carries only `agentcore:project-name` and
  `agentcore:target-name`. The `down` fallback therefore deletes the stack only if
  `agentcore:project-name=AgentcoreDemo`.
- Transaction Search: `transactionSearch false` held; the account's destination was already
  `CloudWatchLogs` (changed 2026-08-31 by someone else, CloudTrail), untouched by this run.
- `agentcore logs` is not needed for evidence: the runtime writes to CloudWatch Logs, which
  `aws logs filter-log-events` reads (verify step 15).
- Teardown with `make agentcore-down` (`remove all --yes` + `deploy --yes`): see the v0.8
  story for the observed result.

## v0.12: VPC mode through the same CLI (2026-10-31, deployed)

- `make agentcore-deploy MODE=VPC` renders `networkMode: "VPC"` with the three private subnets
  and `sg-agentcore-worker` from `.generated/foundation.env`. AgentCore CLI 1.0.1 synthesizes
  `AWS::BedrockAgentCore::Runtime` with `NetworkConfiguration {NetworkMode: VPC,
  NetworkModeConfig {Subnets, SecurityGroups}}`, the existing execution role as `RoleArn` and
  the named endpoint `b_<build7>`. The CLI validates the IDs itself (`Must be a subnet id`), so
  placeholders cannot slip through. The decision stands: no control-plane fallback is needed.
- Live (resumed run): `make agentcore-deploy MODE=VPC` deployed `AgentcoreDemo_default_support_supervisor`
  with exactly that configuration (`get-agent-runtime`: networkMode VPC, 3 subnets on use1-az1/az2/az4,
  `sg-agentcore-worker`, `requireServiceS3Endpoint false`, the existing execution role, idle 120 s) and
  the endpoint `b_4ee7f87` next to `DEFAULT`. The first VPC-mode runtime created the
  service-linked role `AWSServiceRoleForBedrockAgentCoreNetwork`. Decision confirmed; no fallback used.
