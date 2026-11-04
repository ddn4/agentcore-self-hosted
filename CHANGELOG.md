# Changelog

One section per tag. Tags are never moved; fixes go forward as `v0.N.1-…`.

## Unreleased: local only (AWS deployment retired)

The demo now runs only on the laptop: the dev server or the Helm chart on kind, with the
models on Amazon Bedrock through the laptop's SSO profile. The AWS deployment of tags v0.8 and
v0.10–v0.13 (EKS, RDS, internal NLB, Route 53, AgentCore Runtime in a VPC, IAM, Secrets
Manager, budget alarm) was torn down on 2026-11-04, and its code and docs are removed. They
remain readable at those tags.

- Removed: Terraform stacks and the IRSA module, `deploy/k8s/overlays/eks`, `values.eks.yaml`,
  the invocation-role CFN template, the AgentCore CodeZip bundle and CLI tooling, the EKS/RDS/
  Route 53/pause scripts, `demo_testing.agentcore_logs` (CloudWatch), `support_agent.mtls`
  (Secrets Manager client cert), the `eks-pf` profile, `WCI=aws`, `register-version
  --agentcore*`, verify scripts for v0.8 and v0.10–v0.13; the runbook, the implementation
  plan, the verify ledger, the AWS story pages and ADR-0005/0006/0008.
- Kept: the local serverless shape (AgentCore-shaped Worker, the AgentCore API emulator, the
  dev server's WCI, PINNED versions on `support-tq`), kind with mTLS, live and scripted models.
- Changed: `make failure` scales Finance on kind; `case-b.sh` is the plain Case B start;
  `make load` targets the local WCI; `make timeline MERGED=1` has no AgentCore-log lane.
- Console: the `eks-pf` profile, `CONSOLE_ALLOW_AWS`, the `aws` cost tier and the `cloud-b`
  scenario are gone.

## Unreleased: demo console (toward v1.0.0)

A web **demo console** on <http://127.0.0.1:8099> (`make console`) that drives every scenario
and shows it live: Support (`agents`) and Finance (`agent-services`) as two lanes, the Nexus
`send_message` crossing the seam between them, the A2A task state as the headline
(`working → input-required → completed/rejected/failed`), and Support's compute as a rail that
lights up per Worker or AgentCore lifetime and goes empty when it drains. The page has deep
links to the Web UI on :8233. New workspace member `services/demo-console` (package
`demo_console`; Starlette + uvicorn, already in `uv.lock`). No change to Support, Finance or the
contract; Build ID unchanged (`4ee7f87`). Design and API contract: `docs/console/DESIGN.md`;
look: `docs/console/BRAND.md`; story: `docs/story/console.md`.

- Profiles: `local` (dev server; scripted or live models), `kind`, `kind-plaintext`.
  Uses the same envconfig as `make`. Outside `local` it strips `TEMPORAL_TLS`/`TEMPORAL_ADDRESS`,
  and it never passes the parent make's `MAKEFLAGS` to children. It detects a profile mismatch
  from the port-forward pidfiles.
- Process control: the dev server (`WCI=0|1`), payments-mock, the Finance, Support and smoke
  Workers, the AgentCore app and the WCI emulator. Each runs as `make -s <target>` in its own
  process group, is recorded under `.temporal/console/`, and is adopted again after a console
  restart. Stack recipes: local plain, local invoke-local, local WCI emulator, kind laptop
  side, teardown. One-shot jobs: nexus-endpoint, register-version, invoke-local, smoke, card,
  load, wci-history, and the kind cluster lifecycle (confirm first).
- Cost gate on every action (`free | tokens`): actions that spend Bedrock tokens ask first.
- Ledger 72 enforced: the console never queries a Support Workflow (a query on `support-tq`
  would wake an AgentCore session); Support's state comes from history and describe only.
  A unit test checks every route.
- `make console-shots` / `scripts/console-shot.sh`: headless Chrome over the DevTools protocol;
  fixture views at 1440×900 and 390×844, dark and light, into `.temporal/console/shots/`.
  `?fixture=<name>` and `?demo=1` (a recorded Case B replay) work without Temporal.
- Tests: 95 unit tests (fakes only: no AWS, no cluster) and an integration test on the
  two-namespace dev server that drives Case A, Case B approve and reject, and a 409 through the
  console's API with scripted in-process Workers. The kind path is covered by unit tests only;
  it still needs a real run.

## v0.13-scale-to-zero

Case B **entirely in the cloud**: Support on AgentCore (VPC), Finance on EKS. Finance waits in
`input-required`, Support's only session drains (debounce 30 s), the human approves, and the
Nexus completion makes the server's Worker Controller start a **second** AgentCore session that
finishes the reply with the refund ID. **Q15 answered: yes** (task-add-signal +27 ms, InvokeWorker
+56 ms, first Workflow Task on session #2 +5.3 s after the Nexus completion). No AWS resources
created; Build ID unchanged (**`4ee7f87`**); everything left running.

- Demo: `scripts/demo/case-b.sh` with `TEMPORAL_PROFILE=eks-pf` waits for `input-required` and
  for the drain in the AgentCore logs, shows the pending Nexus operation, then asks "approve now?"
  (`CASE_B_APPROVE=approve|reject|no`). `approve.sh APPROVE_NUDGE=1` (Q15 fallback a; not needed).
- `make timeline WF=… MERGED=1`: one wall-clock slide merging the Support, Finance and WCI
  histories plus each AgentCore session (start, connect, drain), with the Q15 deltas; redacted.
- `make load N=10` (`scripts/demo/load.sh`, Case A ×10 or `LOAD_KIND=ping`): 10/10 in 55 s, no WCI
  failure, sessions drained and back to 0. The runbook's N=50 is **deferred to v1.2**.
- `make e2e TEMPORAL_PROFILE=eks-pf` asserts the WCI invocation counts: sessions and `InvokeWorker`
  per Case (A 1, B 2, reject 2). `demo_testing.agentcore_logs` reads sessions from the runtime's
  log streams (one per session). `make refund-idempotency` (same key → same refund on EKS),
  `make ui-shot` (headless Chrome; Agent Card visible in the UI; Q9: the deployment page's
  Compute column stays empty for `aws-agentcore` on UI 2.54.1).
- Findings: a query on a Workflow with no poller wakes a session; the WCI sometimes invokes once
  more while a Worker is still polling (after a Case; 3 extra idle sessions during the N=10 load);
  no re-invoke after a terminate when the Worker is reachable (ledger 44: the v0.8 storm came from
  an unreachable Worker). Reported in `docs/ask-temporal.md`.
- Ledger 28, 29, 44 closed.

## v0.12-agentcore-worker

Support on AgentCore Runtime in **VPC mode**, registered from `main` as the serverless Worker
Deployment Version `support-supervisor:4ee7f87` on the EKS server; the WCI (IRSA
`temporal-server-wci`) assumes `Temporal-AgentCore-Worker` with the External ID and invokes it.
Ping and Case A ran with zero other pollers. Build ID **`4ee7f87`** (was `fd7af7a`). Applied in
us-east-1 and **left running** for v0.13.

- AWS: `30-agentcore-iam` with `create_dev_wci_role = false` (`temporal-dev-wci` deleted;
  `[profile temporal-dev-wci]` in `~/.aws/config` now unused); secret
  `temporal/agentcore-worker-mtls` (EKS CA, 180 d); runtime `AgentcoreDemo_default_support_supervisor`
  + endpoint `b_4ee7f87` via the AgentCore CLI (VPC, existing execution role, idle 120 s); CFN
  `temporal-agentcore-worker` trusting `temporal-server-wci`.
- Fix: NLB `temporal-frontend` **cross-zone load balancing on** (`values.eks.yaml`, `make
  eks-temporal`): with it off, an AgentCore ENI that resolved the NLB node of the AZ without a
  frontend pod timed out connecting (ledger 70).
- Tooling: `make agentcore-deploy MODE=VPC`, `make agentcore-network`, `make agentcore-logs`,
  `make flow-report`, `make export-agentcore-cert CLUSTER=eks CONFIRM=yes|CHECK=1`, `make
  invoke-role`, `make register-version MODE=--agentcore-auto CLOUD=1`,
  `scripts/verify/v0.12-agentcore-worker.sh` (jq/JSON shapes fixed on its first live run; NLB
  cross-zone check). `redact.sh`: the Secrets Manager suffix rule only masks a trailing suffix.
- Support: the AgentCore Worker logs where and how it connected. No Workflow change.
- Answers: Q13 (no `--allow-no-pollers` needed, even with no poller ever seen), Q6 on 1.32.1,
  Q5 production form, 5.4 (CLI VPC + named endpoint + existing role), global profile with the
  execution role, AWS Q2 (S3 prefix list, `logs`, `secretsmanager`, `bedrock-runtime`, NLB).
  New: the WCI does not retry a failed task-queue registration (ledger 71, ask Temporal).

## v0.11-temporal-and-finance-on-eks

Temporal 1.32.1 on EKS with RDS, an internal NLB and IRSA, then Finance on EKS and the hybrid
demo (laptop Support → EKS Finance over Nexus). Applied in us-east-1 and **left running**.
Build ID unchanged (`fd7af7a`: no Support input touched).

- Server (commit 1): `deploy/temporal/values.eks.yaml` layered on the unchanged v0.9 values
  (RDS TLS with `caFile` + `enableHostVerification` on both stores, Secret `rds-ca` from the
  official us-east-1 bundle mounted for the server and the schema hook Job, user `temporal_user`,
  512 shards, frontend ×2 behind an internal TCP NLB with ip targets and
  `agentcore-demo-nlb-temporal-frontend`, IRSA `temporal-server-wci` on `server.worker` only);
  a generated `.generated/values.eks.ids.yaml` carries the RDS address and role ARN.
  `deploy/k8s/overlays/eks/{cert-issuers,temporal-certs,temporal-network}` (the v0.9 bases).
  Scripts `create-db-user.sh` (password → Secrets Manager `temporal/rds-temporal-user` + k8s
  Secret; role created over `verify-full` TLS), `rds-snapshot.sh`, `route53-alias.sh`;
  `scripts/eks.sh` `rds-ca`, `certs`, `values`, `temporal` (dry-run=server, install --timeout
  900s), `netpol-nlb` (the NLB ENIs' /32s), `pf`/`pf-stop`. envconfig profile `eks-pf`;
  `make certs-pull/pf TEMPORAL_PROFILE=eks-pf` (`.certs/eks/`). 20-platform: SG rule
  `nlb_from_nodes` (in-VPC checks through the NLB).
- Finance (commit 2): `deploy/k8s/overlays/eks/temporal-workers` (ECR images, IRSA annotation,
  HTTPS egress narrowed to the VPC, pods spread over nodes) plus a generated layer;
  `make finance-image` (linux/arm64, push to ECR), `make finance-deploy`
  (`FINANCE_BEDROCK_MODEL_ID=` for the `us.` fallback), `make finance-scale`, `make failure`
  (`scripts/demo/failure.sh`).
- Live: schemas 1.19 / 1.14 on PostgreSQL 16.15, every DB session TLS, `s_client` through the NLB
  shows the cert-manager cert and the client-cert request, mTLS with/without cert, NetworkPolicy,
  IRSA (`assumed-role/temporal-server-wci`, `agent-services-worker`), Bedrock global/us allowed and
  another model denied, Q16 on 1.32.1, hybrid Case A and Case B (approve) handled by EKS pods
  (gate **G4**), failure demo recovers, light Q18 re-check.
- `scripts/verify/v0.11-temporal-and-finance-on-eks.sh`; `make infra-static` renders the EKS
  values and overlays; 4 new unit tests; `test_vpc_render` made hermetic against an exported
  `foundation.env`; `redact.sh`/`scrub-check.sh` cover Route 53 zone IDs and NLB names.
- Ledger 39, 40 resolved, 41 resolved for IRSA, 12/33 re-checked, 68/69 recorded. ADR-0006 and
  ADR-0007 v0.11 sections.

## v0.10-aws-foundation

The AWS foundation in Terraform (us-east-1, go-ahead G5), applied and **left running** for
v0.11. Build ID unchanged (no Support input touched).

- `infra/terraform/10-network`: VPC `temporal-customer-support` (10.40.0.0/16) with private
  subnets placed by **AZ ID** (use1-az1/az2/az4; vpc module 6.7.3 takes AZ IDs directly), one
  NAT gateway (one EIP), flow logs (7 d), interface endpoints `bedrock-agentcore`,
  `bedrock-agentcore-control`, `sts`, `secretsmanager`, `bedrock-runtime`, `logs` (private DNS,
  2 AZs), S3 gateway endpoint, security groups `agentcore-demo-{agentcore-worker,
  nlb-temporal-frontend,vpce,rds}` (Role tag = the runbook's `sg-*` names), private zone
  `temporal-customer-support.internal`.
- `infra/terraform/20-platform`: EKS `temporal` 1.37 (eks module 21.29.0; IRSA; public endpoint
  = the laptop /32 from a gitignored tfvars; VPC CNI `enableNetworkPolicy`), 2 × m7g.large
  (arm64), AWS Load Balancer Controller 3.6.2 and cert-manager v1.21.3 by `helm_release`, RDS
  `temporal-db` PostgreSQL **16.15** db.t4g.medium single-AZ encrypted (`rds.force_ssl=1`, RDS-
  managed master password), IRSA roles `temporal-server-wci` and `agent-services-worker`
  (`infra/terraform/modules/irsa-role`, one ServiceAccount each), ECR
  `agentcore-demo/{finance-agent,payments-mock}` (`force_delete`).
- `make check-quotas` (read-only pre-flight incl. CDKToolkit and IAM simulation of
  `CreateServiceLinkedRole` / `PassRole`), `make render-config` (`.generated/foundation.{json,env}`),
  `make eks-kubeconfig eks-env eks-status` (dedicated `.temporal/kubeconfig-eks`), `make pause`
  / `resume` and guarded `make tf-destroy STACK=…|foundation` (all with `DRY_RUN=1`),
  `make tf-plan ARGS=`.
- `scripts/verify/v0.10-aws-foundation.sh`: AZ IDs, endpoints, zone, SGs, EKS, NetworkPolicy
  enforcement, private DNS for AWS APIs, psql `sslmode=require` from a pod (and `disable`
  refused), live IRSA positive/negative, second plan clean, dry runs.
- `redact.sh` / `scrub-check.sh`: SSO role suffix, EKS OIDC and endpoint IDs, RDS endpoint IDs.
- ADR-0006 written (demo sizing; PostgreSQL 16.15 because RDS no longer offers 16.6). Ledger 37
  and 38 resolved.

## v0.8-wci-handshake

First AWS contact (us-east-1, go-ahead G5). The first attempt stopped at the IAM apply
(permission policy); the resumed run did steps 2–9 the same day. Build ID `fd7af7a`.

- Applied and kept: `00-guardrails` (budget USD 200/month at 80 % forecasted / 100 % actual,
  SNS topic, alarm on `AWS/Bedrock-AgentCore` `ActiveSessionCount`), secret
  `temporal/agentcore-external-id`, `30-agentcore-iam` (execution role + `temporal-dev-wci`).
- Created and deleted the same day: PUBLIC probe runtime `AgentcoreDemo_default_support_supervisor`
  (CLI stack `AgentCore-AgentcoreDemo-default`), invocation-role stack `temporal-agentcore-worker`.
- Real WCI handshake from the dev server: AssumeRole with External ID per invoke,
  `GetAgentRuntimeEndpoint` in `ValidateSpec`, `InvokeAgentRuntime` → 14 sessions, each
  fails fast to `127.0.0.1` and releases; wrong External ID → AccessDenied.
- Q15 first AWS look: yes (invoke 4 ms after `NexusOperationCompleted`). Q9 partly (UI 2.54.1
  has no AgentCore renderer). Q13 not answered here.
- Finding: the WCI re-invokes every ~60–68 s (`PullStats`) while it sees a backlog, also for
  4.5 min after the Workflow was terminated (asked of Temporal).
- `scripts/agentcore-deploy.sh`: runtime name `<project>_<target>_<runtime>`; `down` checks the
  CLI stack's `agentcore:project-name` tag and deletes the runtime's leftover log groups.
- `scripts/verify/v0.8-wci-handshake.sh`: no AWS mutations inside; gate 2 on the make-created
  resources; part 3 (`VERIFY_PART=3`) checks the teardown; millisecond `--since` timestamps;
  UI bundle crawl for Q9; `delete-version` retry.
- `redact.sh`: runtime IDs in log-group names, Secrets Manager ARN suffixes.
- Tooling from the first attempt: Terraform 1.17.0, tflint v0.64.0 (sha256), AgentCore CLI
  1.0.1, `tf.sh` (saved-plan apply), CI `make tf-static`; CodeZip ships only `pyproject.toml`.
- ADR-0005 (outcome), ADR-0008 (live CLI facts); ledger 22, 25, 44, 45, 61 resolved, 29 first
  look yes, 28 partly, 21 not answered here; `docs/ask-temporal.md` v0.8 questions.
- v0.7 deferred notes: `agentcore_worker.py` docstring, `connect_with_timeout` rationale.

## v0.9.2-live-bedrock

The live half of every laptop tag, on Opus 5.5 (closes ledger 49; also 7, 8, 10, 17). No
Workflow code changed; Support's Build ID moves to `0015bea` (tests and fixtures are hashed).

- Live replay fixtures `support-1042.json`, `support-2077-approved.json`.
- `opus_params` checks the model ID in the URL; `nested_agents` exits 2 at HEAD instead of
  hanging; `test_live_a2a` Case B approves through the stub Finance.
- `docs/versions.md`: effort latency measured.

## v0.9.1-laptop-complete

Acceptance of the laptop demo on a clean slate, from the README alone (offline: the AWS SSO
session was expired). No Temporal code changed; Build ID `32b04fb` unchanged.

- README: "Run the demo on your laptop" (prerequisites, four scenarios with live and offline
  variants, clean-up) and a v0.9.1 row in the story table.
- `make env` for bare `temporal` commands (they ignored `config/temporal.toml` and failed on
  the v0.7 and v0.9 story steps); v0.7 story: wait for `Healthy` before the WCI path.
- Ledger: "Still open for the AWS tags" summary; item 49 records this run.

## v0.9-temporal-on-kind-mtls

The real server on the laptop (chart temporal-1.7.0, server 1.32.1 on kind), then mTLS
everywhere from a free cert-manager CA. v0.8-wci-handshake was skipped in this run (it needs
AWS resources and go-ahead G5; its items move to v0.12/v0.13).

- `deploy/`: kind cluster + in-cluster postgres:16; chart values `postgresql` / `base`
  (1.32.1, namespaces from values, WCI constrained to `agents`) / `tls` / `kind` (4 shards);
  cert-manager chain exactly as runbook 2.6 and one client cert per identity; Finance (2
  pods) + payments-mock in `temporal-workers` with egress and frontend-ingress
  NetworkPolicies; overlays `kind` (Bedrock via `aws-creds`) and `kind-scripted`.
- Finance and payments-mock Dockerfiles (uv workspace, arm64); the Finance Worker now drains
  on SIGTERM (it ignored it as PID 1).
- `scripts/kind.sh` and Make targets `kind-up kind-temporal kind-workers kind-aws-creds
  certs-pull pf kind-down cert-renew ...`; envconfig profiles `kind` (mTLS, Route 53
  server name) and `kind-plaintext`; per-Worker client certs on `TEMPORAL_PROFILE=kind`.
- Makefile: a profile from the environment beats `.env`; the dev server's
  `TEMPORAL_TLS=false` / address no longer leak into other profiles.
- `make infra-static` is real (helm template + kustomize -> kubeconform -strict) and part of
  `make check` (330 tests, 319 at v0.7); CI installs kubeconform and builds the images.
- Answers: chart keys and labels (ledger 30), WCI constraint + logs and dynamic config
  reload without restarts (24, 31), arm64 (32), Q10 server cert hot-reload: yes; Q18 SDK
  file certs: read once at connect (33), internode EKUs + frontend needs `client auth` for
  the chart's own pods (34), kindnet enforces NetworkPolicy (36), Q16 on 1.32.1 (12).
- G3 passed: Case B over mTLS with a Finance pod deleted mid-wait. G2 decision recorded.
- Optional Secrets Manager path and live Bedrock not run (no AWS; ledger 35, 49).

## v0.7-serverless-shape

The Support Worker in AgentCore Runtime shape, on the laptop: built to die, pinned to its build.

- `support_agent.agentcore_app`: `BedrockAgentCoreApp` entrypoint (single-start guard,
  `add_async_task` -> `create_task` -> immediate ack, WCI payload checked against this
  version, `complete_async_task` in `finally`); one Worker lifetime = mTLS (no-op locally) ->
  connect with `TEMPORAL_CONNECT_TIMEOUT_SECONDS` -> versioned Worker on `support-tq`
  (`support-supervisor:<BUILD_ID>`, PINNED, 120 s drain) -> `ActivityTracker` debounce
  (`AGENTCORE_DEBOUNCE_SECONDS`, 20 s locally). `PingWorkflow`, `worker_versioned`, `mtls`.
  Shape of samples-python `bedrock_agentcore/strands_agent`.
- Content-hash Build ID (`scripts/build-id.sh`; refuses dirty inputs). CodeZip bundle
  (`make agentcore-bundle`: flat, vendored contract, `requirements.txt` + generated
  `pyproject.toml`, `BUILD_ID`, rendered `agentcore.json` with endpoint `b_<build7>`);
  `make agentcore-bundle-test` (fresh 3.12 venv, serve, connect timeout, 43 MB arm64 zip).
- **The dev server contains the WCI** (ledger 23). `make dev-server WCI=1` +
  `scripts/agentcore_emulator.py` let the real WCI invoke the local Worker with no AWS
  contact; `register-version.sh`, `wci-history.sh`, `serverless_report.py`.
  `make invoke-local` is the plan's "I am the WCI" path.
- Answers: 7.1 `create` is required; `create-version` invokes the runtime (ledger 20); Q13
  informational: no `--allow-no-pollers` needed for a version with a compute config (21);
  Q4/CodeZip local half (22); Q6 WCI IDs/namespace/activities locally (25); Q15 local first
  look: a Nexus completion wakes the WCI (29).
- Tests: 319 in `make check` (283 at v0.6): entrypoint contract with a fake app, debounce
  with a fake clock, mTLS, PingWorkflow on a versioned Worker and the full entrypoint
  lifecycle on `start_local`, Build ID stability in a throwaway clone, `agentcore.json`
  rendering, emulator routes. CI builds and tests the bundle.
- `scripts/verify/v0.7-serverless-shape.sh`: Case B across two Worker lifetimes, pinned to
  one Build ID, invoked by hand and by the WCI.
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.8).

## v0.6-human-in-the-loop

Finance's human approver, and a caller that can wait for it at no cost.

- Finance (`FinanceAgentTaskWorkflow`): over $100 or at HIGH risk the A2A task goes
  `input-required` and waits, durably, for `approve`/`reject` signals
  (`ApproverDecision(approver, comment)`; sync handlers; decisions outside `input-required`
  ignored) for at most `FINANCE_APPROVAL_TIMEOUT` (default 24h, labelled timer); no decision
  → `failed`; approve → refund with `approved_by=<approver>`. "Current details" in the Web UI
  show the task state. The timeout travels in the task's memo (ledger 58).
- Contract: `APPROVAL_WAIT_LIMIT` (24 h) published in `finance_a2a_api` and in the Agent
  Card (v1.1.0); Finance refuses a larger `FINANCE_APPROVAL_TIMEOUT`.
- Support: `SUPPORT_NEXUS_SEND_TIMEOUT` (default 25h) is the third Workflow input and the
  `send_message` budget (ADR-0009). `rejected` → apology, `failed` → apology + escalation,
  `NexusOperationError` → apology (explicit "Next step" in the tool result; prompts updated).
- ADR-0002 (HITL in code, not Strands `interrupt()`) and ADR-0009 (timeout budget) accepted.
- Tests: 283 in `make check` (208 at v0.5). Finance time-skipping: approve, reject, 24 h
  timeout, RISK=HIGH on $40, idempotency key, out-of-place signals. Support (stub Finance with
  the approval rule): approve/reject in both modes, `failed` → escalation, caller budget too
  short → Nexus timeout with the Finance task orphaned. Two namespaces: approve, reject,
  Support Worker stopped mid-wait, end-to-end timeout (5s/60s). `tests/test_timeout_budget.py`.
- Demo: `make approve`, `make reject`, `make pending`, `make result`, `make approval-report`;
  `make case-b` starts and returns; `scripts/verify/v0.6-human-in-the-loop.sh`
  (`VERIFY_REAL_WAIT_MINUTES=31`: approved after 31 real minutes with Support stopped).
- Found: no time skipping through an open Nexus operation on the v1 test server (ledger 56,
  `make spike-nexus-wait`); the server caps a Nexus budget at the caller's execution timeout
  (57); an optional trailing `run()` parameter untypes a dataclass input (58). Reported in
  `ask-temporal.md`.
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.7).

## v0.5-a2a-over-nexus

The Supervisor asks Finance itself: A2A over Nexus from inside the agent loop.

- `support_agent/finance_peer.py`: Agent Card discovery (`get_agent_card`) before the first
  Supervisor turn; `send_message` with a 25 h budget and operation summaries naming peer,
  order, amount and mode; `NexusOperationError` reported to the Supervisor, not raised.
- `ask_finance_agent`: async in-workflow `@tool` awaiting the Nexus operation (**Q19a: works**;
  G1 not triggered). `SUPPORT_A2A_MODE=handoff`: unforced capture tool `submit_refund_request`,
  Nexus call in `run()`, second Supervisor turn. The mode is a Workflow input.
- Prompt-drift guard: one re-prompt if eligible and Finance not contacted. New `a2a` query.
- Tests: 199 in `make check` (172 at v0.4). Time-skipping with a stub Finance behind the real
  contract (`demo_testing.finance_stub`); two-namespace Supervisor → real Finance; crash after
  Finance answered (no repeat); sync-tool probe; v0.5 replay fixtures; v0.3 fixtures kept as
  negatives. `test_live_a2a.py` (`-m bedrock`, Case A ×5) and `tests/e2e/test_refund.py`
  (`make e2e`, profile-driven).
- Demo: `make case-a|case-b` honour `SUPPORT_A2A_MODE`; `make timeline`, `make a2a`,
  `make task-state WF=support-…`, `make e2e`; `make dev` runs the Support Worker and applies
  the endpoint. `scripts/verify/v0.5-a2a-over-nexus.sh`, `scripts/check_a2a_run.py`.
- Found: a sync Strands `@tool` fails quietly inside a Workflow (`asyncio.to_thread`);
  reported to Temporal. ADR-0003 part 2 accepted; ledger 15–17 resolved (17 live half pending).
- Verifier fixes: the drift guard no longer reads "NOT ELIGIBLE"/"NOT-ELIGIBLE" as eligible
  (`is_eligible()`, unit-tested; 208 tests); `make task-state` works on a rejected task
  (CLI prints empty artifacts as `null`).
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.6).

## v0.4-finance-agent

Another team's agent behind a published A2A contract, reached over Nexus (no LLM on the caller).

- `finance_a2a_api`: A2A 0.3.0-shaped dataclasses (snake_case; A2A 1.0.0 deltas in its
  README), `@nexusrpc.service FinanceAgentA2A` (`get_agent_card` sync, `send_message`
  workflow-backed), `FINANCE_AGENT_CARD` + `render_markdown()`. Round-trips through the default
  and pydantic converters, 7b.9 CLI JSON, schema-snapshot gate, Agent Card golden.
- Finance (`agent-services`): `FinanceAgentA2AHandler` (`a2a-task-<context_id>`, `USE_EXISTING`),
  `FinanceAgentTaskWorkflow` (A2A task states, `get_task` query, auto-approve only: over $100 or
  HIGH risk -> `rejected` until v0.6), Finance Refund agent (`fraud_score`, `refund_history`),
  approval rule in code, idempotent `issue_refund`; `make worker-finance[-scripted]`.
- `payments-mock`: idempotent refund API (stdlib), `make payments-mock` on :8081.
- Support: `A2ASmokeWorkflow` + `make worker-smoke` (`nexus-smoke-tq`); Nexus operation summaries.
- `make nexus-endpoint` (endpoint description = Agent Card), `make smoke`, `make card`
  (standalone Nexus Operation from the CLI), `make task-state`, `scripts/a2a_timeline.py`,
  `make dev` runs payments-mock, Finance and smoke Workers.
- Dev server and test fixture enable standalone Nexus operations
  (`nexusoperation.enableStandalone=true`; off by default in CLI 1.9.1, contrary to the docs).
- Contract module uses real annotations (string annotations broke decoding in the caller's sandbox).
- Answered: A2A field names (pinned 0.3.0), Q16 CLI on 1.32.0, Q21 (yes), Nexus on time-skipping
  (v1 yes, v2 no), 10 s sync limit, idempotency. ADR-0004 implementation section.
- Tests: 172 in `make check` (62 at v0.3), incl. cross-namespace Nexus with the real endpoint
  name; replay fixtures `smoke-1042`, `a2a-task-smoke-1042`. `scripts/verify/v0.4-finance-agent.sh`.
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.5).

## v0.3-sub-agents

Support's own team of agents, used as tools, in-process.

- `support_agent/agents.py`: Order Lookup agent (`lookup_order`) and Return Policy agent
  (`get_return_policy`) as `TemporalAgent`s, each built fresh inside a hand-written
  in-workflow `@tool`; Supervisor (runbook 5.6 prompt minus Finance) uses only those two
  tools. `summary` per agent attributes every `invoke_model` in the UI.
- Workflow query `sub_agent_runs`; return policy rule 5 (over $100: Finance manager
  approval), so Case B (2077) is escalated.
- Q20 resolved (ADR-0003 part 1): nesting works; Strands `Agent.as_tool()` writes durable
  timers in a Workflow (cancel-signal poll), one shared sub-agent refuses parallel calls.
  Reported in `docs/ask-temporal.md`; runbook annotated.
- `demo_testing`: per-system-prompt `support_responder`, Activity summaries in history
  helpers, probe Workflows for `as_tool()` and a shared instance.
- Tests: nested sequence/attribution, per-agent prompts and tools, Case B, parallel
  sub-agent calls, failing sub-agent, crash inside a sub-agent turn, re-recorded
  dev-server fixtures, v0.2 fixture asserted non-replayable, live `bedrock` tests.
- `spikes/nested_agents.py` (`make spike-nested`), `make case-b`, `make sub-agents`,
  `scripts/verify/v0.3-sub-agents.sh` (includes v0.2's carried live steps).
- Live Bedrock still deferred (AWS SSO session expired; ledger 49 carried to v0.4).

## v0.2-durable-agent

An LLM loop as a Workflow that survives a crash.

- `SupportSupervisorWorkflow`: `TemporalAgent(model="bedrock")` with `lookup_order` and
  `get_return_policy` as `activity_as_tool`; model Activity heartbeat 10 s, bounded retry,
  UI summary; `transcript` query. Sync data Activities on a thread pool; mock orders
  1042/2077/3301 and the return policy.
- `worker_factory.build_worker()` + `make worker-support` (`support-dev-tq`).
- `BEDROCK_EFFORT` → `output_config.effort` (not sent at the default `medium`), both services.
- `demo_testing`: `ScriptedModel` under the `"bedrock"` key (text, tool calls, `Hang`,
  responder functions, delay), history helpers, structured-output probe Workflow.
- Tests: activities, scripted agent loop, pinned key, unknown model key, tool errors,
  replay (committed history from a real `kill -9` run), crash safety across two Workers,
  structured output's forced retry.
- Spikes: `opus_params.py` (wire, 400s, effort field and latency) and
  `opus_structured_output.py` (forced vs capture tool) through the plugin's own
  `invoke_model` Activity. **Live run pending** (AWS SSO session expired).
- Demo: `make case-a`, `make worker-support-scripted`, `make record-histories`, `make spikes`;
  `scripts/verify/v0.2-durable-agent.sh` (`kill -9` a real Worker mid-`invoke_model`).
- ADR-0001 amended: effort field; no `structured_output_model` on Opus 5.5 (capture tool).
- Offline wire test (`test_wire_offline.py`): the signed ConverseStream request from the
  pinned model carries the Opus 5.5 ID, no sampling params, `toolChoice` auto, and effort
  only when not `medium`.
- Live Bedrock checks deferred to v0.3 (ledger 49; AWS SSO session expired at this tag).

## v0.1-scaffold

A green, trustworthy repo before the first line of agent code.

- uv workspace (Python 3.12): `packages/finance-a2a-api`, `packages/demo-testing`,
  `services/support-agent`, `services/finance-agent`, `services/payments-mock`.
- Per-service `config.py`: envconfig connection (`TEMPORAL_CONFIG_FILE` /
  `TEMPORAL_PROFILE`), and `StrandsPlugin(models={"bedrock": ...})` pinning
  Opus 5.5 (`global.anthropic.claude-opus-5-5`, `us-east-1`) with no inference
  params (ADR-0001).
- Trivial `HelloWorkflow` + `say_hello` Activity per service, tested on a
  two-namespace dev server (`start_local`, CLI 1.9.1) and on the time-skipping server.
- `demo_testing.fixtures`: session-wide two-namespace dev server, per-namespace clients.
- Makefile: `setup doctor fmt lint types test test-unit test-workflow test-integration
  check scrub-check scrub-history test-live bedrock-ping dev-server dev-server-up
  dev-server-down dev-server-reset dev hello-smoke verify`.
- Scripts: `doctor.sh`, `dev-server.sh`, `bedrock_ping.py`, `hello_smoke.py`,
  `scrub-check.sh` (gitleaks if installed, grep scrub always), `redact.sh`,
  `verify/v0.1-scaffold.sh`.
- CI workflow file `.github/workflows/ci.yml` (`make check` + gitleaks; not run yet: no remote).
- Docs: `versions.md`, `verify-ledger.md` (seeded from plan §6; 6 items resolved),
  `ask-temporal.md`, ADR-0001, ADR-0004 (Nexus Activity backing descoped, user-confirmed),
  ADR-0007, story `v0.1-scaffold.md`.
- Verification pass: `test_plugin_worker.py` per service (HelloWorkflow on the Worker with the
  pinned StrandsPlugin and pydantic converter); `hello_smoke.py` uses the plugin; gitleaks tree
  scan limited to committable files.
