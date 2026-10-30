# Runbook: Self-hosted Temporal Server 1.32 on AWS with Serverless Workers on Amazon Bedrock AgentCore Runtime

> **Audience:** the engineer doing the work. This runbook gives instructions only. It does not provision anything.
> **Date of research:** 2026-10-29. Versions referenced: Temporal Server **1.32.1**, Helm chart **temporal-1.7.0**, Temporal CLI **≥1.8.3** (1.9.1 current), Temporal UI **2.54.1**, AgentCore CLI (`@aws/agentcore`) **1.0.1**.
> **⚠️ Verify:** marks items I could not confirm from official docs, or that come from source code, community posts or inference. Confirm them before you rely on them.
>
> **Demo design (decided 2026-10-29):**
> - **Scenario: customer-support refund, with an agent-to-agent (A2A) call** (§1.4).
>   - The **Support Supervisor** is a Strands agent in `agents`, running on **AgentCore Serverless Workers**. Its sub-agents run as tools.
>   - It **asks a separate Finance agent** for a refund, using A2A-style messages over the Nexus endpoint `finance-agent`.
>   - The **Finance agent** is its own Strands agent, owned by another team. It runs in `agent-services` on **long-running EKS Workers** and has its own approval step with a person in the loop.
>   - The Finance agent is **not** a child Workflow. Child Workflows must live in the caller's namespace. This is a cross-namespace, agent-to-agent request.
>   - See §1.3, §1.4, 5.6, Phase 7b and Phase 8 step 7.
> - **Worker auth: mTLS** (option (c) in 2.7).
> - **Server AWS identity: IRSA** (4.1).
> - **One AWS account, one VPC.**
>
> **Environment (decided 2026-10-29):**
> - **Region: `us-east-1`.** Every AgentCore feature is available there. AgentCore subnets must be in `use1-az1`, `use1-az2` or `use1-az4`.
> - **Greenfield:** a new VPC and a new EKS cluster, built with **Terraform**.
> - **DNS:** Route 53 private hosted zone `temporal-customer-support.internal`.
>   - Frontend: `temporal-frontend.temporal-customer-support.internal:7233`
>   - UI: `temporal-ui.temporal-customer-support.internal`
> - **CA: free, in-cluster.** cert-manager uses a self-signed root, then a `CA` ClusterIssuer issues every certificate. There is no ACM Private CA (2.6).
> - **Model:** Claude Opus 5.5 for every Strands agent, through the global cross-Region inference profile `global.anthropic.claude-opus-5-5` (3.2).

---

## 0. TL;DR

1. **The frontend DNS name is `temporal-frontend.temporal-customer-support.internal`**, in the Route 53 private hosted zone `temporal-customer-support.internal`. Create the zone first. The certificates, the Worker config and the UI all depend on this name.
2. Build the AWS foundation:
   - a VPC with private subnets in **AgentCore-supported AZ IDs**
   - VPC endpoints
   - an EKS cluster sized for production
   - RDS PostgreSQL
   - **SQL visibility with password auth** is the default path. OpenSearch is optional.
3. Install Temporal with the official Helm chart `temporal-1.7.0`:
   - Override the image tags to `1.32.1`.
   - Choose `numHistoryShards` once, because you cannot change it later.
   - Put **every** setting (persistence, TLS, NLB, auth, dynamic config, ServiceAccounts) into one values set before the first install.
4. Expose the frontend (gRPC 7233) through an **internal TCP NLB**, then point a Route 53 alias at it. Use mTLS end to end.
5. **Authorization model: decided, mTLS (option (c) in 2.7).**
   - Use the noop authorizer plus mTLS plus network isolation, and record that as an accepted risk.
   - Give the two Worker types **distinct client-cert CNs** (`agentcore-worker`, `eks-nexus-worker`). This keeps the hardening path open: option (b), a custom ClaimMapper.
6. Turn on the Worker Controller with dynamic config: `workercontroller.enabled`, `compute_providers.enabled: [aws-agentcore]`, `scaling_algorithms.enabled: [no-sync]`.
7. Give **only the Temporal worker-service pods** an AWS identity through IRSA, using `server.worker.serviceAccount`. That identity may only `sts:AssumeRole` the invocation role.
8. Build a **Python** Worker with `BedrockAgentCoreApp`. Deploy it to AgentCore Runtime in **VPC mode** with a **named endpoint**. Endpoint names allow no hyphens, for example `temporal_v1`.
9. Create the **invocation role** from Temporal's **self-hosted** CloudFormation template, which uses an External ID.
10. Register the Worker with `temporal worker deployment create-version --aws-agentcore-*`. Run `set-current-version` **immediately** afterwards; if it fails for lack of pollers, add `--allow-no-pollers`.
11. **Set up the Nexus side (Phase 7b):**
    - Create the `agent-services` namespace.
    - Deploy the Nexus handler Worker to EKS. It reaches the frontend through the in-cluster Service and uses a cert-manager client cert.
    - Create the Nexus Endpoint `finance-agent` → `agent-services` / `agent-services-tq`.

    Nexus needs **no** server config on 1.32.
12. Start the caller Workflow with no Worker running. Watch the WCI invoke AgentCore, the Nexus call reach EKS, and a **second** AgentCore session start when the Nexus result comes back (Phase 8). Keep the rollback and teardown steps (Phase 11) ready.

---

## 1. Overview and architecture

### 1.1 What you are building

```
                         AWS account / Region (AgentCore-supported)
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │  VPC (private subnets in supported AZ IDs)   Route 53 private zone:              │
 │                                              temporal-frontend.temporal-… → NLB  │
 │  ┌──────────────────────── EKS: namespace "temporal" ────────────────────────┐   │
 │  │                                                                           │   │
 │  │  frontend ◄──┐   history    matching ──(sync-match fail / backlog)──┐     │   │
 │  │   :7233      │                                                      ▼     │   │
 │  │              │                              worker service (system workers)│  │
 │  │              │                              └─ Worker Controller Instance │   │
 │  │              │                                 (WCI, 1 per Deployment Ver)│   │
 │  │              │                                 AWS creds: IRSA, SA        │   │
 │  │              │                                 "temporal-worker" only     │   │
 │  │  web UI, admintools                                     │                 │   │
 │  └──────────────┼──────────────────────────────────────────┼─────────────────┘   │
 │                 │ (A) gRPC/mTLS 7233                       │ (B) HTTPS 443        │
 │        ┌────────┴──────────┐                    ┌─────────┴───────────────────┐  │
 │        │ Internal NLB (TCP │                    │ VPC interface endpoints:    │  │
 │        │ passthrough)      │                    │  sts, bedrock-agentcore,    │  │
 │        └────────▲──────────┘                    │  bedrock-agentcore-control  │  │
 │                 │                               └─────────┬───────────────────┘  │
 │                 │                                         │ 1. sts:AssumeRole     │
 │                 │                                         │    (+ExternalId)      │
 │                 │                                         │ 2. GetAgentRuntime-   │
 │                 │                                         │    Endpoint           │
 │                 │                                         │ 3. InvokeAgentRuntime │
 │                 │                                         ▼                       │
 │  ┌──────────────┴─────────────── AgentCore Runtime (VPC mode, ENIs) ──────────┐  │
 │  │  Named endpoint  …/runtime-endpoint/<name, no hyphens> → pinned version     │  │
 │  │  Session (microVM, up to 2 vCPU / 8 GB, ≤8h)                                │  │
 │  │   BedrockAgentCoreApp  /invocations, /ping                                  │  │
 │  │    └─ async task "temporal-worker" → temporalio Worker (versioned)          │  │
 │  │         polls Task Queue → runs Workflows/Activities → idle → drain → exit  │  │
 │  │  Execution role: Secrets Manager (mTLS material), Bedrock InvokeModel, Logs │  │
 │  └─────────────────────────────────────────────────────────────────────────────┘  │
 │                                                                                  │
 │  RDS PostgreSQL (temporal, temporal_visibility)   [optional] Amazon OpenSearch   │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 How a task reaches an AgentCore-hosted Worker

1. Your application starts a Workflow on a Task Queue through a normal Temporal Client. The AgentCore invocation payload is **not** Workflow input.
2. Matching tries to hand the Task to a poller. If no Worker is polling (a sync-match failure), or a backlog builds up, Matching signals the **Worker Controller Instance (WCI)**. The WCI is a system Workflow. One runs per Worker Deployment Version that has a compute provider.
3. The WCI gets AWS credentials from the AWS SDK v2 default chain and calls `sts:AssumeRole` on your **invocation role**, passing the **External ID**. It then calls:
   - `GetAgentRuntimeEndpoint` on the control plane
   - `InvokeAgentRuntime` on the data plane, against the **named endpoint ARN**

   The payload is `{deploymentName, buildId}`, and the qualifier is the endpoint name. The Region comes from the ARN.
4. The Runtime's `@app.entrypoint` handler:
   - registers an async task (`app.add_async_task("temporal-worker")`)
   - starts a long-running `temporalio` Worker in an asyncio task
   - returns an acknowledgment right away
5. The Worker connects to the frontend over 7233 (mTLS), polls the **versioned** Task Queue and processes Tasks.
6. The Worker shuts down when no Activity has started or finished for the debounce window. The sample uses `AGENTCORE_DEBOUNCE_SECONDS`, default 60 s.
   - Shutdown is graceful; the sample sets `graceful_shutdown_timeout` to 120 s.
   - The Worker then calls `app.complete_async_task(task_id)`, and the session idles out.
7. Scaling uses the event-driven `no-sync` algorithm, the same one Lambda uses. The WCI invokes sessions as they are needed. It does not keep a pool of a target size.

### 1.3 Two-namespace Nexus demo design

| | Namespace `agents` (caller) | Namespace `agent-services` (handler) |
|---|---|---|
| Code | `SupportSupervisorWorkflow`: the Strands Support Supervisor plus sub-agent tools. Its `ask_finance_agent` tool calls `workflow.create_nexus_client(...).execute_operation(...)` | The `FinanceAgentA2A` Nexus service handler plus `FinanceAgentTaskWorkflow` (the Strands Finance agent) |
| Worker runtime | AgentCore Runtime (Serverless Worker, invoked by the WCI) | EKS Deployment (long-running pollers) |
| Task Queue | `<TASK_QUEUE>` (versioned, compute config) | `agent-services-tq` |
| Worker Versioning | Mandatory (Serverless Workers need it) | Optional; unversioned is fine for a first pass (see 7b.5) |
| WCI enabled | Yes (`workercontroller.enabled` constrained to `agents`) | **No.** Keep the constraint, so the WCI never manages this namespace. |

```
 Client ──start──► [agents] SupportSupervisorWorkflow
                      │ (1) Workflow Task: no poller → WCI → InvokeAgentRuntime → AgentCore session #1
                      │ (2) Caller Worker (AgentCore) runs the Workflow Task → ScheduleNexusOperation
                      ▼
            Nexus Endpoint "finance-agent"   (server-side routing, inside the Temporal Service)
                      ▼
       [agent-services] task queue agent-services-tq
                      │ (3) EKS handler Worker (polling all the time) takes the Nexus Task
                      │     → starts the handler Workflow (or standalone Activity) → returns an operation token
                      │ (4) Handler work completes on EKS
                      ▼
            Completion callback → temporal://system (internal, no URL config on 1.32)
                      ▼
       [agents] NexusOperationCompleted → new Workflow Task
                      │ (5) Session #1 has probably drained by now → no poller → WCI → AgentCore session #2
                      ▼
                 SupportSupervisorWorkflow completes
```

**Points the demo shows:**
- **Workers never talk to each other.** Both kinds of Worker only open **outbound** gRPC connections to the frontend. Nexus routing, and delivery of the completion callback, happen inside the Temporal Service.
- **How the two Workers connect:**

  | | AgentCore Worker | EKS Worker |
  |---|---|---|
  | Network path | ENI in the VPC subnet → `sg-agentcore-worker` → internal NLB → frontend pods | Pod → kube-proxy / ClusterIP `temporal-frontend.temporal.svc.cluster.local:7233` → frontend pods (never leaves the cluster network) |
  | DNS name dialled | `temporal-frontend.temporal-customer-support.internal` (Route 53 private zone) | `temporal-frontend.temporal.svc.cluster.local` |
  | Where its mTLS material comes from | Secrets Manager, fetched at session start (execution role) | cert-manager `Certificate` → Kubernetes Secret mounted as files, renewed automatically |
  | Lifecycle | Invoked on demand by the WCI; exits after the idle debounce; one Runtime version per Build ID | Always running; scaled by replicas or HPA; rolling deploys |
  | Firewalling | Security groups only (ENIs) | Kubernetes NetworkPolicy, plus node or pod SGs |
  | AWS identity (if it calls Bedrock and similar) | AgentCore execution role | IRSA ServiceAccount (optional; see 7b.3) |
  | Cold start | Session start on every invocation; visible in Workflow Task latency | None |

- **Visible serverless behaviour.** If the Nexus operation takes longer than `AGENTCORE_DEBOUNCE_SECONDS`, the caller's AgentCore session drains while it waits. The completion produces a new Workflow Task, and the WCI starts a **new** session. In the refund scenario (§1.4), this happens naturally: a small refund is approved automatically (one session). A large refund waits for a human approver (two sessions).
- **Nexus is single-cluster only.** The endpoint registry is not replicated, which fits this single-cluster design.

### 1.4 Demo scenario: the refund, Support agent → Finance agent (A2A over Nexus)

**The story the viewer sees.** A customer writes: *"Order #1042, my headphones arrived broken. I want my money back."*

1. The **Support Supervisor** agent (team: Customer Support; runs on AgentCore) works out what happened, using its own sub-agents as tools:
   - **Order Lookup agent:** finds the order, item, price and delivery date (mock order data).
   - **Return Policy agent:** decides whether the request is eligible ("damaged items are always refundable within 30 days").
2. Support **cannot move money**. It does what a human support rep would do: it **asks the Finance agent**. This is an explicit agent-to-agent request, made through the tool `ask_finance_agent`.
3. The **Finance Refund agent** (team: Finance; runs on EKS, next to the payments system) treats the request as an A2A **task**:
   - It scores fraud risk, using its own tools and data that Support never sees.
   - It applies approval limits.
   - **≤ $100:** it issues the refund itself → task `completed`.
   - **> $100:** it pauses for a human approver → task `input-required`. When the approval signal arrives, it issues the refund → `completed`. If the approver rejects it → `rejected`.
4. Finance replies with an A2A message and a **refund confirmation artifact**. Support drafts the customer reply.

**Why an A2A call, not sub-agents or child Workflows:**

| | Sub-agents (Order Lookup, Return Policy) | Finance agent |
|---|---|---|
| Owner | Support team | Finance team |
| Where it runs | Inside the Supervisor's Workflow (tools of the same agent) | Its own Workflow, in **another namespace**, on another fleet (EKS) |
| How it's called | Strands "agents as tools" (in-process) | **A2A request over Nexus**: a named endpoint, a published contract, and no shared code or namespace access |
| Can it pause for a person? | No (it's a quick lookup) | Yes, `input-required` for hours, while Support's AgentCore session scales to zero |
| A child Workflow? | No | **No.** Child Workflows must be in the caller's namespace. Nexus is the cross-namespace primitive. |

**How the A2A concepts map onto Temporal.** The payloads are *shaped like* the A2A protocol, so viewers recognise them. The transport is Nexus, which makes the call durable.

| A2A concept | In this demo |
|---|---|
| **Agent Card** (name, description, skills) | The sync Nexus operation `get_agent_card`. Its text is also the **Nexus Endpoint description**, so it shows in the Web UI's Nexus registry. |
| **Message** (role, parts, contextId) | Input to the `send_message` Nexus operation: `A2AMessage(role="user", parts=[text, data{order, amount, reason}], context_id=<support workflow id>)` |
| **Task** (id, contextId, status.state, artifacts) | The Finance handler Workflow `FinanceAgentTaskWorkflow`, ID `a2a-task-<context_id>`. The query `get_task` returns the A2A Task object. |
| Task states `submitted → working → input-required → completed / rejected / failed` | Updated by the handler Workflow and visible through the `get_task` query. The terminal Task is the Nexus operation result. |
| Response to `input-required` | Temporal signal `approve` / `reject` to the Finance Workflow (from the UI, the CLI or an approver app) |
| **Artifact** | `refund_confirmation` (data part: refund ID, amount, approved_by) |

⚠️ Verify: this is **A2A-shaped, not A2A wire-compatible**. Field names follow the A2A spec's concepts (Agent Card, Message/Part, Task/TaskState, Artifact). Check them against the spec version you show. To expose the Finance agent to non-Temporal A2A clients too, put a thin A2A JSON-RPC gateway in front that calls the same Nexus endpoint. That is out of scope for this demo. *Resolved in v0.4: pinned to A2A 0.3.0 (snake_case), with the A2A 1.0.0 renames listed in `packages/finance-a2a-api/README.md`.*

**How to make the A2A hop obvious in the demo:**
- **Names:**
  - endpoint `finance-agent`
  - service `FinanceAgentA2A`
  - operations `get_agent_card` / `send_message`
  - tool `ask_finance_agent`
  - handler Workflow `FinanceAgentTaskWorkflow`, with ID `a2a-task-…`
- **Caller history:** `NexusOperationScheduled` (`FinanceAgentA2A.send_message`, endpoint `finance-agent`) appears between the Supervisor's `invoke_model` activities. The UI link jumps to the Finance Workflow in **the other namespace**.
- **Agent-to-agent conversation:** the Supervisor's system prompt says *"You cannot issue refunds. For any refund, call `ask_finance_agent` with the order facts, and wait for Finance's decision."* The Finance reply text (for example *"Approved refund R-88213 for $450 after manager approval"*) is fed back to the Supervisor as the tool result. Show both agents' messages side by side.
- **Visible task state:** during Case B, run `temporal workflow query --namespace agent-services --workflow-id a2a-task-<id> --type get_task`. It returns `state: input-required`, while `agents` shows the AgentCore session gone.


---

## 2. Support status and caveats (read first)

| Question | Answer | Confidence |
|---|---|---|
| Are AgentCore Serverless Workers supported on **self-hosted**? | **Yes.** Temporal publishes a dedicated self-hosted setup page, and the docs point users to "try it through self-hosting". Access on Temporal Cloud is by request only. | Confirmed |
| Is 1.32 enough? | **Yes.** v1.32.0 is the first server release with "Serverless Workers – AgentCore Runtime (pre-release)". Use **1.32.1**, the latest patch. | Confirmed |
| Maturity | **Pre-release.** The docs say the APIs "may change in backwards-incompatible ways". Lambda and Cloud Run Serverless Workers are Public Preview. Pin the server, CLI and SDK versions you test with. | Confirmed |
| Which SDKs? | **Python only.** The other SDKs support Lambda and/or Cloud Run only. | Confirmed |
| Required server flags | `workercontroller.enabled: true`, `workercontroller.compute_providers.enabled: [aws-agentcore]`, `workercontroller.scaling_algorithms.enabled: [no-sync]`. All of them are **off or empty by default**. | Confirmed |
| Required CLI | Temporal CLI **≥ v1.8.3**, which adds the `--aws-agentcore-*` flags. Use 1.9.1. | Confirmed |
| Worker Versioning | **Mandatory.** It has been enabled by default on the server since 1.28, so it needs no extra server config. | Confirmed |
| Multi-cloud / multi-cluster replication | The docs say Serverless Workers do not support Temporal Cloud **Multi-cloud Replication**, and call this a temporary Public Preview restriction. Whether self-hosted multi-cluster replication (XDC / global namespaces) works with Serverless Workers is **UNVERIFIED**. Avoid it, or test failover behaviour explicitly. | Confirmed (Cloud) / Unverified (self-hosted) |
| Temporal's tutorial, blog and Strands sample | Written for **Temporal Cloud**. The sample's `mk-invoke-role.sh` and its IAM template trust Cloud's principal, and its client code sets `tls=bool(TEMPORAL_API_KEY)`. **You must adapt them for self-hosted.** | Confirmed |
| Nexus on self-hosted 1.32 | **On by default, with no config.** `system.enableNexus` was removed in 1.31 (Nexus is always on). Since 1.31, callbacks for Worker targets use `temporal://system`, so neither `callback.endpoint.template` nor `callback.allowedAddresses` is needed. The chart already enables the frontend HTTP port (7243). Nexus is single-cluster only. | Confirmed |
| Nexus Operation backed by a workflow (`@workflow_run_operation`) | Supported by the Python SDK. Sync operations (`@sync_operation`) must respond within **10 s**. | Confirmed |
| Nexus Operation backed by a standalone Activity (`TemporalOperationHandler` + `start_activity`) | **Pre-release / experimental.** It needs dynamic config `activity.enableCallbacks: true`. The samples target a special dev-server build. Whether stock server **1.32.1** supports it is ⚠️ UNVERIFIED. Treat it as an optional variant (7b.4). | Partly confirmed |
| AgentCore CLI commands in Temporal's docs and sample | Written for an older `@aws/agentcore` CLI. CLI 1.0.x has no `validate`, and `status` takes no `--runtime` or `--type` flag. This runbook uses 1.0.x syntax. Before running anything, check `agentcore --version` and `agentcore <cmd> --help`. | Confirmed (CLI command.md) |

**Resolved and still-open research points:**
- **Which server service makes the AWS calls.** The docs say only "the Temporal Service". The server source (v1.32.0, `service/worker/fx.go`) imports `go.temporal.io/auto-scaled-workers/wci/workercomponent` and registers `wcicomponent.Module`. That confirms the WCI is wired into the **worker service**. Chart 1.7.0 supports per-component ServiceAccounts (`server.worker.serviceAccount`), so you can scope the AWS identity to the worker pods only (see 4.1).
- **IRSA vs EKS Pod Identity.** Temporal's docs cover neither. Pod Identity session tags are **transitive**, which the EKS docs confirm. Whether the invocation-role trust then needs `sts:TagSession` is UNVERIFIED. **This runbook recommends IRSA.**
- **Advanced dynamic config keys:**
  - `workercontroller.maxInstances` exists in the auto-scaled-workers version that 1.32.1 vendors (`v0.2.0-1.32.0.158.0`). It is namespace-scoped, defaults to 100, and limits the number of Worker Deployment Versions with a compute config per namespace. It does **not** limit sessions.
  - ⚠️ Verify: `…aws.intermediary_roles`, `…periodic_validation_interval_s` and `…hook.min_signal_interval_*` exist only in source code.
- **No self-hosted "Validate Connection" button.** That button is Cloud-only. On self-hosted, the WCI validates when a version is created and periodically after that. You check the result in the WCI Workflow history.
- **Authorization vs mTLS-only Workers.** These conflict, and you must decide how to resolve it before Phase 7 (see 2.7).

---

## 3. Prerequisites

### 3.1 Tools (on your workstation or CI)
| Tool | Version |
|---|---|
| AWS CLI v2 | current, with permissions for EKS, IAM, CloudFormation, EC2/VPC, RDS, Route 53, Secrets Manager, AgentCore |
| Terraform | ≥ 1.5, AWS provider v5+, for the VPC, EKS, RDS, Route 53 and IAM (Phase 1). ⚠️ Verify the provider version that adds the `aws_bedrockagentcore_*` resources, if you also want the Runtime in Terraform (5.1). |
| kubectl, Helm v3 | compatible with your EKS version |
| Temporal CLI | **≥ 1.8.3** (1.9.1 recommended; 1.9.0+ is needed for the 1.32 `temporal nexus operation` commands) |
| Node.js | **≥ 20.12**, for the AgentCore CLI (`npm install -g @aws/agentcore`, currently 1.0.1) |
| Python | ≥ 3.10 (the Runtime uses `PYTHON_3_12`); `uv` recommended |
| AWS CDK | bootstrapped in the target account and Region (the AgentCore CLI uses CDK) |
| Docker | not needed for CodeZip builds (only for `agentcore dev` or container builds) |
| jq, openssl, psql client | on your workstation or a bastion. The admin-tools image has only `sh` and the Temporal binaries. |

> If `pip install bedrock-agentcore-starter-toolkit` was ever run on this machine, uninstall it. Its `agentcore` command shadows the npm CLI.

### 3.2 AWS account and Region
- **Region: `us-east-1` (decided).** It supports every AgentCore feature. Full list: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html.
- In VPC mode, AgentCore subnets must be in **supported AZ IDs**. In us-east-1 those are `use1-az1`, `use1-az2` and `use1-az4`. AZ *names* (`us-east-1a` and so on) map to different AZ IDs in each account, so choose subnets by **AZ ID**. In Terraform, filter `data "aws_availability_zones"` on `zone_id`. Check a subnet with:
  ```bash
  aws ec2 describe-subnets --subnet-ids <subnet> --query 'Subnets[0].AvailabilityZoneId'
  ```
- **Permissions for the deploying principal** (moved here from troubleshooting):
  - `iam:CreateServiceLinkedRole`, so VPC mode can create the service-linked role for `network.bedrock-agentcore.amazonaws.com`
  - `iam:PassRole` on the Runtime execution role
  - permission to assume the CDK bootstrap roles
- **Bedrock model (decided): Claude Opus 5.5 via `global.anthropic.claude-opus-5-5`.** All three agents use it: Support Supervisor, its sub-agents, and Finance.
  - **Inference profile only (in us-east-1).** On `bedrock-runtime`, the bare model ID `anthropic.claude-opus-5-5` works in eu-west-2 only. From us-east-1, use a geo or global profile. The global profile routes requests to any supported commercial Region. The Converse API, which Strands' `BedrockModel` uses, is supported. (Source: the AWS model card, 3.2 references.)
  - **Data residency: `us.anthropic.claude-opus-5-5` exists** (confirmed on the AWS model card). Called from us-east-1, it routes to **us-east-1, us-east-2 and us-west-2** only. To keep inference in the US, set `BEDROCK_MODEL_ID=us.anthropic.claude-opus-5-5`. No code changes are needed, but the IAM changes below are. Geo profiles may cost more than global for recent Claude models; check the Bedrock pricing page. Confirm the routing for your account with:
    ```bash
    aws bedrock get-inference-profile --region us-east-1 --inference-profile-identifier us.anthropic.claude-opus-5-5 \
      --query "models[].modelArn"
    ```
  - **Tool choice.** On Bedrock, Opus 5.5 accepts only the `tool_choice` types `auto` and `none`. Forced tool use is rejected with a 400. ⚠️ Verify how Strands implements `structured_output_model`. If it forces a tool call, the fallback in 5.6 that uses `structured_output_model=RefundRequest` will fail on this model. Test it in the first spike.
  - **Model access.** In the Bedrock console, open the model catalog and enable Anthropic models for this account. This includes the one-time Anthropic use-case form.
  - **Pin the model explicitly.** Without a `models` mapping, the Strands plugin falls back to Strands' default `BedrockModel()`, which is a different model (`global.anthropic.claude-sonnet-4-6` as of strands-agents 1.42). Pin Opus 5.5 with `StrandsPlugin(models={"bedrock": lambda: BedrockModel(model_id=…)})`, and set `model="bedrock"` on every `TemporalAgent` (5.6, 7b.5).
  - **Request parameters.** Do **not** pass `temperature`, `top_p`, `top_k` or a disabled-thinking config to `BedrockModel`. Opus 5.5 rejects them with a 400. To trade quality for latency, lower the effort setting instead; the default is `medium`.
  - **IAM for a global profile.** Grant `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on **both** of these:
    - the profile: `arn:aws:bedrock:us-east-1:<acct>:inference-profile/global.anthropic.claude-opus-5-5`
    - the foundation model in **every** Region: `arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5`

    This applies to the AgentCore execution role (4.2) and the `agent-services-worker` IRSA role (7b.3). An SCP that denies Regions outside us-east-1 will break the global profile. ⚠️ Verify against the Bedrock cross-Region inference IAM docs.
  - **IAM for the `us.` profile instead:** grant the same actions on the profile `arn:aws:bedrock:us-east-1:<acct>:inference-profile/us.anthropic.claude-opus-5-5`. Grant them on the foundation model only in the destination Regions: `arn:aws:bedrock:{us-east-1,us-east-2,us-west-2}::foundation-model/anthropic.claude-opus-5-5`, which is three ARNs. An SCP may limit you to those three Regions.

### 3.3 AgentCore quotas that affect this design
| Quota | Default |
|---|---|
| Active sessions | 5,000 in us-east-1 and us-west-2, 2,500 elsewhere (adjustable) |
| New sessions | 25 per second per account (adjustable) |
| Data-plane requests | 1,000 TPS per account (adjustable). Shared across all data-plane APIs (InvokeAgentRuntime, StopRuntimeSession and others), and counted against the **runtime-owner** account for cross-account calls. |
| Control-plane Get APIs (GetAgentRuntime, GetAgentRuntimeEndpoint, which the WCI calls) | 150 TPS per account, **not adjustable** |
| Per-session hardware | Up to 2 vCPU / 8 GB per session. This is a maximum and cannot be adjusted. You are billed on actual consumption. |
| Session max lifetime | 8 h on microVMs; idle timeout default 900 s (range 60–28800 s) |
| **Endpoints per agent** | **10** (adjustable). You need one endpoint per Worker Deployment Version, so clean up old ones. |
| Artifact size | container image 2 GB; CodeZip 250 MB compressed / 750 MB uncompressed |

### 3.4 Decisions to make before you start
- [ ] `numHistoryShards`. This value is **immutable**. Use 512 for small production, and 1024 or more if you expect growth.
- [x] **Decided:** frontend DNS name `temporal-frontend.temporal-customer-support.internal`, in the Route 53 private hosted zone `temporal-customer-support.internal`. The UI is `temporal-ui.temporal-customer-support.internal`. Cert SANs depend on these names.
- [x] **Decided:** Region `us-east-1`, a greenfield VPC and EKS cluster, built with Terraform (Phase 1).
- [x] **Decided:** model `global.anthropic.claude-opus-5-5` for every agent (3.2).
- [ ] Visibility store. **Default for this runbook: SQL advanced visibility on PostgreSQL with password auth.** OpenSearch 2+ is the alternative for higher volume and needs extra IAM (see 2.2).
- [x] **Decided:** AgentCore and EKS share the **same account and VPC**.
- [x] **Decided:** authorization model **(c)**, mTLS (see 2.7). The other options were (a) JWT for everyone and (b) a custom ClaimMapper on cert identity.
- [x] **Decided:** server AWS identity through **IRSA** (see 4.1).
- [x] **Decided: CA layout.** A **free, in-cluster cert-manager CA** (2.6) issues **all** certs from **one** root, so the single `ca.crt` the chart uses trusts all of them (see 2.5). That covers:
  - internode
  - frontend
  - the AgentCore Worker client
  - the **EKS Worker client**
  - the UI client
  - the operator client
- [ ] **Caller side:** the namespace `agents`, a deployment name, a Task Queue and a Build ID scheme.
- [ ] **Handler side:** the namespace `agent-services`, the Task Queue `agent-services-tq`, the Nexus Endpoint name `finance-agent` and the Kubernetes namespace `temporal-workers`. These are the names this runbook uses; rename consistently if you change them.
- [ ] Nexus Operation backing for the handler: **Workflow** (default) or **standalone Activity** (experimental; see 7b.4).
- [ ] AgentCore endpoint naming scheme. Names must match `[a-zA-Z][a-zA-Z0-9_]{0,47}`, so **no hyphens**. For example `temporal_v1`, `temporal_v2`.

---

## 4. Implementation phases

### Phase 1 — AWS foundation

Phase 1 is greenfield and uses **Terraform**. The snippets below use the community modules `terraform-aws-modules/vpc/aws` and `terraform-aws-modules/eks/aws`. They are sketches, not a tested root module. The AWS CLI commands elsewhere in this phase show the equivalent resources, so map each one to its Terraform resource (`aws_rds_cluster`/`aws_db_instance`, `aws_vpc_endpoint`, `aws_iam_role` and so on).

**1.1 VPC**
- At least two **private** subnets in AgentCore-supported AZ IDs. In us-east-1, use `use1-az1`, `use1-az2` and `use1-az4`. They hold the AgentCore ENIs and can also be the EKS subnets.
- Turn on VPC DNS support and DNS hostnames.
- For egress, use NAT, or VPC endpoints if the VPC has no internet path (see 1.4).
- Turn on VPC Flow Logs, which AgentCore best practice recommends.
  ```hcl
  provider "aws" { region = "us-east-1" }

  locals { agentcore_az_ids = ["use1-az1", "use1-az2", "use1-az4"] }
  data "aws_availability_zones" "agentcore" {
    filter {
      name   = "zone-id"
      values = local.agentcore_az_ids
    }
  }

  module "vpc" {
    source  = "terraform-aws-modules/vpc/aws"
    version = "~> 5.0"                       # ⚠️ Verify the current major
    name    = "temporal-customer-support"
    cidr    = "10.40.0.0/16"
    azs             = data.aws_availability_zones.agentcore.names   # names that map to the supported AZ IDs
    private_subnets = ["10.40.0.0/19", "10.40.32.0/19", "10.40.64.0/19"]
    public_subnets  = ["10.40.192.0/24", "10.40.193.0/24", "10.40.194.0/24"]  # NAT only
    enable_nat_gateway   = true
    single_nat_gateway   = true              # demo cost; use one per AZ for production
    enable_dns_support   = true
    enable_dns_hostnames = true
    enable_flow_log                      = true
    create_flow_log_cloudwatch_log_group = true
    create_flow_log_cloudwatch_iam_role  = true
    private_subnet_tags = { "kubernetes.io/role/internal-elb" = "1" }   # internal NLB discovery
  }
  ```
- **Route 53 private hosted zone** (mandatory). Create it now and associate it with the VPC that EKS and AgentCore use:
  ```hcl
  resource "aws_route53_zone" "internal" {
    name = "temporal-customer-support.internal"
    vpc { vpc_id = module.vpc.vpc_id }
  }
  ```
  The CLI equivalent is `aws route53 create-hosted-zone --name temporal-customer-support.internal --vpc VPCRegion=us-east-1,VPCId=<VPC> --caller-reference $(date +%s) --hosted-zone-config PrivateZone=true`.

  You create the alias record that points at the NLB after the Helm install (2.4). Put the frontend name in the frontend certificate's SAN list now.
- Why `.internal`? ICANN reserved it for private use, so it never resolves publicly. Avoid `.local`, which macOS sends to mDNS.

**1.2 EKS**
- **Create the cluster.** Use a currently supported EKS Kubernetes version, and run managed node groups across the three supported AZs.
  ```hcl
  module "eks" {
    source  = "terraform-aws-modules/eks/aws"
    version = "~> 20.0"                      # v21 renames inputs (name, kubernetes_version, endpoint_*); ⚠️ Verify
    cluster_name    = "temporal"
    cluster_version = "<supported-k8s-version>"
    vpc_id          = module.vpc.vpc_id
    subnet_ids      = module.vpc.private_subnets
    cluster_endpoint_private_access      = true
    cluster_endpoint_public_access       = true                  # demo: lets Terraform/Helm run from your workstation
    cluster_endpoint_public_access_cidrs = ["<your-ip>/32"]      # set public access to false for production, and use a bastion or VPN
    enable_irsa                              = true              # IAM OIDC provider for IRSA (4.1, 7b.3)
    enable_cluster_creator_admin_permissions = true
    eks_managed_node_groups = {
      temporal = {
        instance_types = ["m6i.xlarge"]      # size to your load
        min_size = 3, max_size = 9, desired_size = 3
      }
    }
  }
  ```
  ⚠️ Verify: instance size and counts against your load test.
- Install the **AWS Load Balancer Controller**, which handles the NLB annotations. Use the `helm_release` resource, or Helm directly. It needs its own IRSA role; the `terraform-aws-modules/iam` IRSA submodule has a preset for it.
- The **IAM OIDC provider** for IRSA comes from `enable_irsa = true` above. The alternative is the Pod Identity Agent (see §2).
- **cert-manager** (required: it is the CA, see 2.6). Install it with Helm (`jetstack/cert-manager`, `crds.enabled=true`) before Phase 2.
- Optional: **external-dns**, which maintains the Route 53 alias for the NLB automatically (see 2.4).

**1.3 Persistence (RDS PostgreSQL)**
- Temporal tests **PostgreSQL** 13.18, 14.15, 15.10 and 16.6 (plugin `postgres12` or `postgres12_pgx`), and **MySQL** 8.0.19+ (plugin `mysql8`).
- ⚠️ Verify: Aurora and PostgreSQL 17 are **not** on Temporal's tested list. The community does run Aurora, but Temporal sends all reads to the writer, so reader instances do nothing.
- **Create the instance:**
  ```bash
  aws rds create-db-subnet-group --db-subnet-group-name temporal-db \
    --db-subnet-group-description "Temporal" --subnet-ids <subnet-a> <subnet-b> <subnet-c>
  aws rds create-db-instance --db-instance-identifier temporal-db \
    --engine postgres --engine-version 16.6 --db-instance-class db.r6g.large \
    --allocated-storage 100 --storage-type gp3 --storage-encrypted \
    --multi-az --backup-retention-period 7 --deletion-protection \
    --db-subnet-group-name temporal-db --vpc-security-group-ids <sg-rds> \
    --master-username postgres --manage-master-user-password \
    --db-parameter-group-name <your-pg16-param-group> --no-publicly-accessible
  ```
  - ⚠️ Verify: RDS PostgreSQL 15+ sets `rds.force_ssl=1` by default (source: community Slack). Leave it on and use TLS, as in 2.2.
  - Aurora/RDS MySQL may need `connectAttributes: tx_isolation: "READ-COMMITTED"`.
- **Create the login and grants.** admin-tools has no `psql`, so use a bastion or a temporary pod:
  ```bash
  kubectl run psql --rm -it -n temporal --image=postgres:16 -- \
    psql "host=<rds-endpoint> user=postgres dbname=postgres sslmode=require"
  ```
  ```sql
  CREATE USER temporal_user WITH PASSWORD '<strong-password>';
  ALTER USER temporal_user CREATEDB;            -- needed for createDatabase: true
  -- or, if you create the DBs yourself:
  -- CREATE DATABASE temporal OWNER temporal_user;
  -- CREATE DATABASE temporal_visibility OWNER temporal_user;
  ```
  You need two databases: `temporal` and `temporal_visibility`. The chart creates them when `createDatabase: true`.
- **Create the Kubernetes Secret** the chart references:
  ```bash
  kubectl create namespace temporal
  kubectl create secret generic temporal-db-secret -n temporal --from-literal=password='<strong-password>'
  ```
  Production alternative: keep the password in Secrets Manager and sync it with External Secrets or the ASCP / Secrets Store CSI driver. ⚠️ Verify: the exact install and sync syntax.
- **RDS CA bundle.** Download it and store it as a Secret. It is mounted in 2.2.
  ```bash
  curl -o rds-ca.pem https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem
  kubectl create secret generic rds-ca -n temporal --from-file=rds-ca.pem=rds-ca.pem
  ```

**1.4 VPC endpoints (needed if the subnets have no NAT; recommended in any case)**

| Who needs it | Endpoint service |
|---|---|
| Temporal server (WCI) | `com.amazonaws.us-east-1.bedrock-agentcore` (data plane), `com.amazonaws.us-east-1.bedrock-agentcore-control` (control plane) |
| Temporal server (WCI, IRSA) | `com.amazonaws.us-east-1.sts`. ⚠️ Verify: needed in practice, but the Temporal docs do not name it. |
| AgentCore Runtime ENIs, **CodeZip** build (recommended here) | `s3` **Gateway** endpoint. Its policy must allow the service-owned code bucket `arn:aws:s3:::acr-code-*-us-east-1-an` and `…/*` with condition `aws:PrincipalServiceName = bedrock-agentcore.amazonaws.com`. Also `logs`. |
| AgentCore Runtime ENIs, **Container** build only | `ecr.api`, `ecr.dkr`, plus the `s3` gateway |
| Worker code | `secretsmanager`, `sts`, `bedrock-runtime` (for model calls). ⚠️ Verify: whether `bedrock-runtime` is the only endpoint the model calls need. |

```bash
for svc in bedrock-agentcore bedrock-agentcore-control sts secretsmanager bedrock-runtime logs; do
  aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --vpc-id <VPC> \
    --service-name com.amazonaws.us-east-1.$svc \
    --subnet-ids <subnet-a> <subnet-b> --security-group-ids <sg-vpce> --private-dns-enabled
done
# Only for Container builds:
# for svc in ecr.api ecr.dkr; do ... same command ... ; done

# S3 gateway endpoint (attach to the private route tables)
aws ec2 create-vpc-endpoint --vpc-endpoint-type Gateway --vpc-id <VPC> \
  --service-name com.amazonaws.us-east-1.s3 --route-table-ids <rtb-a> <rtb-b> \
  --policy-document file://s3-gw-policy.json
```
Example `s3-gw-policy.json`. Copy the exact statement from the AgentCore VPC doc (agentcore-vpc.html#agentcore-vpc-endpoints), and add any other buckets your workloads need:
```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject",
  "Resource":["arn:aws:s3:::acr-code-*-us-east-1-an","arn:aws:s3:::acr-code-*-us-east-1-an/*"],
  "Condition":{"StringEquals":{"aws:PrincipalServiceName":"bedrock-agentcore.amazonaws.com"}}}]}
```

**1.5 Security groups**

| SG | Rules |
|---|---|
| `sg-agentcore-worker` | **Egress** TCP 7233 to `sg-nlb-temporal-frontend`. Egress 443 to `sg-vpce`, covering `bedrock-runtime`, `sts`, `secretsmanager`, `logs`, the AgentCore endpoints and ECR. Egress 443 to the **S3 managed prefix list** (`pl-…`), because gateway endpoints are reached by prefix list, not by SG (⚠️ Verify: AgentCore docs do not say whether this is required). **No inbound rules**: Runtime ENIs only start outbound connections. |
| `sg-nlb-temporal-frontend` | Ingress TCP 7233 from `sg-agentcore-worker` and from other approved clients (bastion or VPN, client apps) |
| Temporal pod/node SG | Ingress TCP 7233 from `sg-nlb-temporal-frontend`. LBC adds this when `manage-backend-security-group-rules: "true"`; otherwise add it yourself. If you use security groups for pods or NetworkPolicies, allow intra-cluster traffic between Temporal pods on **6933–6939** (membership) and **7233–7239** (gRPC). Egress 443 to `sg-vpce` (or NAT), 5432 to `sg-rds`, and 443 to OpenSearch if you use it. |
| `sg-rds` | Ingress TCP 5432 from the Temporal pod/node SG (and from the bastion/psql pod) |
| EKS Nexus Worker pods | In-cluster only. They reach `temporal-frontend.temporal.svc:7233` over the pod network, so no NLB rule is needed. If the node SG or pod SGs restrict east-west traffic, allow 7233 from the worker pods to the frontend pods. If the handler calls AWS (for example Bedrock), allow egress 443 to `sg-vpce`. Add a Kubernetes NetworkPolicy (7b.6). |
| `sg-vpce` | Ingress 443 from `sg-agentcore-worker` and from the Temporal pod/node SG |

These rules are a derived design. The SG behaviour itself comes from AgentCore's documentation.

---

### Phase 2 — Install Temporal Server 1.32 on EKS (Helm)

> **Values files.** Use exactly two files and pass both to every `helm install` or `helm upgrade`:
> 1. `values.postgresql.yaml`: your edited copy of the chart's `charts/temporal/values/values.postgresql.yaml` from GitHub (tag `temporal-1.7.0`; the Helm repo does not serve it).
> 2. `my-overrides.yaml`: **every** snippet in 2.1–2.10, Phase 3 and 4.1, merged under a single top-level `server:` / `web:` / `admintools:` key.
>
> Merge everything **before the first install**. That way the dynamic config, ServiceAccounts and TLS are in place from the start.

**2.1 Pin chart and images.** Chart `temporal-1.7.0` has `appVersion 1.32.0`. Override the images to the 1.32.1 patch:

```yaml
# my-overrides.yaml
server:
  image:
    tag: 1.32.1
admintools:
  image:
    tag: "1.32.1"
```
⚠️ Verify: chart 1.7.0 was not explicitly tested with 1.32.1. It is a patch release, so it is expected to work. *Resolved in v0.9: works on kind (all pods 1.32.1); only the pod label `app.kubernetes.io/version` still shows 1.32.0. The chart package itself contains `values/values.postgresql.yaml`.*

> Chart 1.x installs **no** databases, Elasticsearch, Prometheus or Grafana. Docs that mention "chart 0.73.x", `server.configMapsToMount` or `setConfigFilePath` refer to the old 0.x chart. Ignore them.

**Sizing (production).** The chart defaults to a single replica, which is not production-ready:
```yaml
server:
  frontend: { replicaCount: 3, resources: { requests: { cpu: "1", memory: 1Gi }, limits: { memory: 2Gi } } }
  history:  { replicaCount: 3, resources: { requests: { cpu: "2", memory: 4Gi }, limits: { memory: 8Gi } } }
  matching: { replicaCount: 3, resources: { requests: { cpu: "1", memory: 1Gi }, limits: { memory: 2Gi } } }
  worker:   { replicaCount: 2, resources: { requests: { cpu: 500m, memory: 512Mi }, limits: { memory: 1Gi } } }
```
⚠️ Verify: the per-service keys in chart 1.7.0's values.yaml (`replicaCount`, `resources`, and the PodDisruptionBudget key, if one exists). Otherwise create PodDisruptionBudgets with `minAvailable: 1` per service yourself. Size from your load test. *Resolved in v0.9: `replicaCount`, `resources`, `podDisruptionBudget` and `serviceAccount` exist per component under `server.<service>`.*

**2.2 Persistence values** (in `values.postgresql.yaml`):

```yaml
server:
  additionalVolumes:
    - name: rds-ca
      secret: { secretName: rds-ca }
  additionalVolumeMounts:
    - name: rds-ca
      mountPath: /etc/temporal/rds-ca
      readOnly: true
  config:
    persistence:
      defaultStore: default
      visibilityStore: visibility
      numHistoryShards: 1024        # IMMUTABLE after first start
      datastores:
        default:
          sql:
            createDatabase: true
            manageSchema: true
            pluginName: postgres12
            driverName: postgres12
            databaseName: temporal
            connectAddr: "<rds-endpoint>:5432"
            connectProtocol: tcp
            user: temporal_user
            existingSecret: temporal-db-secret
            secretKey: password
            maxConns: 20
            maxIdleConns: 20
            maxConnLifetime: "1h"
            tls:
              enabled: true
              caFile: /etc/temporal/rds-ca/rds-ca.pem
              enableHostVerification: true
        visibility:
          sql:
            pluginName: postgres12
            driverName: postgres12
            databaseName: temporal_visibility
            # ...same connection and tls fields...
```
- The schema Job and admintools also need the RDS CA mounted. ⚠️ Verify: chart 1.7.0's keys for this. The schema Job may reuse `server.additionalVolumes`; check for `admintools.additionalVolumes` / `additionalVolumeMounts`, and confirm against `templates/server-job.yaml` and the admintools deployment template. *Resolved in v0.9: the schema Job and the namespace Job mount `admintools.additionalVolumes` / `additionalVolumeMounts` (not `server.additionalVolumes`); set the RDS CA in both.*
- **Default path: password auth.** Since 1.31 you can use `passwordCommand` for IAM database auth. The server image has **no** `aws` CLI: it is Alpine with only ca-certificates, tzdata and `temporal-server`. So IAM DB auth needs a helper binary or a custom image, plus `rds-db:connect` (see below).
- **Optional OpenSearch visibility** (not the default):
  - Use the `elasticsearch` datastore shape with `aws-request-signing: {credentialProvider: aws-sdk-default, region: us-east-1}`.
  - Create the schema and index:
    ```bash
    temporal-elasticsearch-tool --ep https://<opensearch-endpoint>:443 --aws-credentials aws-sdk-default setup-schema
    temporal-elasticsearch-tool --ep https://<opensearch-endpoint>:443 --aws-credentials aws-sdk-default create-index --index temporal_visibility_v1
    ```
    The chart's own schema job runs `update-schema --index ""`, then `create-index`, then `update-schema --index $ES_VISIBILITY_INDEX`.
  - The datastore `version:` value for OpenSearch is UNVERIFIED. The release notes only say to configure it "as you would for Elasticsearch", and their examples use v7.
- **Extra IAM if you pick a non-default path.** All server services (history and others) need OpenSearch (`es:ESHttp*`) or `rds-db:connect`, not just the worker service. Put these permissions on a **separate** role bound to the global `serviceAccount`. Do **not** add them to the worker-only WCI role in 4.1. Update the least-privilege checklist item to match.

**2.3 Schema.** By default the chart runs a schema Job on pre-install and pre-upgrade hooks. If you use Flux or Terraform, or want explicit control, set `schema.useHelmHooks: false` and run the steps yourself from `temporalio/admin-tools:1.32.1`:

```bash
temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p 5432 --db temporal create
temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p 5432 --db temporal setup-schema -v 0.0
temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p 5432 --db temporal update-schema -d /etc/temporal/schema/postgresql/v12/temporal/versioned
temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p 5432 --db temporal_visibility create
temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p 5432 --db temporal_visibility setup-schema -v 0.0
temporal-sql-tool --plugin postgres12 --ep $POSTGRES_SEEDS -u $POSTGRES_USER -p 5432 --db temporal_visibility update-schema -d /etc/temporal/schema/postgresql/v12/visibility/versioned
```
- The target schema versions for 1.32 are core **v1.19** and visibility **v1.14**.
- Add the TLS flags as needed: `--tls --tls-ca-file /etc/temporal/rds-ca/rds-ca.pem`.
- The admin-tools image has no bash, psql, curl or jq.
- **Snapshot RDS before you run any schema change** (see Phase 11).

**2.4 Expose the frontend with an internal TCP NLB**, so mTLS passes through to Temporal:

```yaml
server:
  frontend:
    service:
      type: LoadBalancer
      port: 7233
      annotations:
        service.beta.kubernetes.io/aws-load-balancer-type: external
        service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
        service.beta.kubernetes.io/aws-load-balancer-scheme: internal
        service.beta.kubernetes.io/aws-load-balancer-security-groups: <sg-nlb-temporal-frontend>
        service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules: "true"
        external-dns.alpha.kubernetes.io/hostname: temporal-frontend.temporal-customer-support.internal   # if external-dns is installed
```
- Do **not** add `aws-load-balancer-ssl-cert`, and do not use ALB mTLS. Either one terminates TLS, and the frontend would never see the client certificate. ⚠️ Verify: this conclusion is inferred from LBC behaviour. Community reports also describe ALB 502 errors that a TCP NLB fixed.
- **Route 53 alias (mandatory).** After the install, point the name you chose in Phase 1 at the NLB. Use external-dns (the annotation above), or do it by hand:
  ```bash
  NLB_DNS=$(kubectl get svc temporal-frontend -n temporal -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
  NLB_ZONE=$(aws elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='$NLB_DNS'].CanonicalHostedZoneId" --output text)
  aws route53 change-resource-record-sets --hosted-zone-id <private-zone-id> --change-batch '{
    "Changes":[{"Action":"UPSERT","ResourceRecordSet":{"Name":"temporal-frontend.temporal-customer-support.internal","Type":"A",
    "AliasTarget":{"HostedZoneId":"'"$NLB_ZONE"'","DNSName":"'"$NLB_DNS"'","EvaluateTargetHealth":false}}}]}'
  ```

**2.5 TLS / mTLS.** Certificates are `kubernetes.io/tls` secrets with `tls.crt`, `tls.key` and `ca.crt`.

```yaml
server:
  tls:
    internode:
      enabled: true
      secretName: temporal-internode-tls
      mountPath: /etc/temporal/tls/internode
      requireClientAuth: true
      serverName: temporal-internode.temporal-customer-support.internal
    frontend:
      enabled: true
      secretName: temporal-frontend-tls
      mountPath: /etc/temporal/tls/frontend
      requireClientAuth: true          # mTLS for Workers/clients
      serverName: temporal-frontend.temporal-customer-support.internal
```
- Frontend TLS needs **internode TLS** as well, because backend roles call the frontend with their internode certificates. Internode certs must have **no EKUs**, or **both** ServerAuth and ClientAuth.
- **Frontend cert SANs.** The SANs must cover **every name a client dials**:
  - `temporal-frontend.temporal-customer-support.internal`: the Route 53 name the AgentCore Workers, operators and bastion use.
  - `temporal-frontend.temporal.svc.cluster.local` and `temporal-frontend.temporal.svc`: the in-cluster names the EKS Nexus Workers use.

  An alternative is to have the EKS Workers dial the svc name with `TEMPORAL_TLS_SERVER_NAME=temporal-frontend.temporal-customer-support.internal`, which overrides the name checked. Adding the SANs makes the in-cluster path clearer for the demo.
- **How the frontend trusts client CAs.** The chart generates `frontend.server.clientCaFiles` as exactly one file, `<frontend mountPath>/ca.crt` from `temporal-frontend-tls`. You cannot add a CA by listing it in `server.tls`. That one file must trust:
  - the internode certs
  - the **AgentCore Worker client cert**
  - the **EKS Nexus Worker client cert**
  - the UI client cert
  - the operator client cert

  Pick one of these:
  1. **Recommended:** issue all of these certs from the **same** CA as the one in `temporal-frontend-tls`'s `ca.crt`.
  2. Make `ca.crt` a **bundle** (internode CA + Worker CA).
  3. Override the generated list (it is deep-merged) and mount the extra CA. A list override **replaces** the generated list, so include the frontend `ca.crt` path again:
     ```yaml
     server:
       additionalVolumes:
         - name: worker-client-ca
           secret: { secretName: temporal-worker-client-ca }
       additionalVolumeMounts:
         - name: worker-client-ca
           mountPath: /etc/temporal/tls/worker-ca
           readOnly: true
       config:
         tls:
           frontend:
             server:
               clientCaFiles:
                 - /etc/temporal/tls/frontend/ca.crt
                 - /etc/temporal/tls/worker-ca/ca.crt
     ```
- Keep the chart's default **tcpSocket** readiness probes, because gRPC probes fail when TLS is on.
- **Test** from inside the VPC (a bastion or a port-forward, see 7.0) with a client cert from the Worker CA:
  ```bash
  temporal operator namespace list --address temporal-frontend.temporal-customer-support.internal:7233 \
    --tls-cert-path worker.crt --tls-key-path worker.key --tls-ca-path ca.crt \
    --tls-server-name temporal-frontend.temporal-customer-support.internal
  # Add --api-key <JWT> if you chose authorization option (a).
  ```

**2.6 Certificates (decided: a free cert-manager CA in the cluster).** ACM Private CA costs about $400 a month in general-purpose mode, or about $50 a month in short-lived-certificate mode. This design uses cert-manager's built-in issuers instead, which cost nothing:
- a `SelfSigned` issuer creates the root once
- a `CA` ClusterIssuer signs every certificate from that root

The CA issuer fills in `ca.crt` in every Secret it writes, which is the layout the chart (2.5) and the EKS Worker (7b.2) expect.

1. **Install cert-manager** in Phase 1.2: `helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true`.
2. **Create the root and the issuer:**
   ```yaml
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata: { name: selfsigned-bootstrap }
   spec: { selfSigned: {} }
   ---
   apiVersion: cert-manager.io/v1
   kind: Certificate
   metadata: { name: temporal-root-ca, namespace: cert-manager }   # ClusterIssuer CA secrets live in the cert-manager namespace
   spec:
     isCA: true
     commonName: temporal-customer-support Root CA
     secretName: temporal-root-ca
     duration: 87600h                       # 10 years; rotating the root means re-issuing everything
     privateKey: { algorithm: ECDSA, size: 256 }
     issuerRef: { name: selfsigned-bootstrap, kind: ClusterIssuer, group: cert-manager.io }
   ---
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata: { name: temporal-ca }
   spec:
     ca: { secretName: temporal-root-ca }
   ```
3. **Issue the server certificates** in the `temporal` namespace. The secret names match 2.5.
   ```yaml
   apiVersion: cert-manager.io/v1
   kind: Certificate
   metadata: { name: temporal-frontend-tls, namespace: temporal }
   spec:
     secretName: temporal-frontend-tls
     commonName: temporal-frontend.temporal-customer-support.internal
     dnsNames:
       - temporal-frontend.temporal-customer-support.internal   # Route 53 name (AgentCore Workers, operators)
       - temporal-frontend.temporal.svc.cluster.local           # in-cluster name (EKS Nexus Workers)
       - temporal-frontend.temporal.svc
     duration: 2160h
     renewBefore: 360h
     usages: [server auth, digital signature, key encipherment]
     privateKey: { algorithm: ECDSA, size: 256 }
     issuerRef: { name: temporal-ca, kind: ClusterIssuer, group: cert-manager.io }
   ---
   apiVersion: cert-manager.io/v1
   kind: Certificate
   metadata: { name: temporal-internode-tls, namespace: temporal }
   spec:
     secretName: temporal-internode-tls
     commonName: temporal-internode.temporal-customer-support.internal
     dnsNames: [temporal-internode.temporal-customer-support.internal]
     duration: 2160h
     renewBefore: 360h
     usages: [server auth, client auth, digital signature, key encipherment]   # internode needs BOTH EKUs (2.5)
     privateKey: { algorithm: ECDSA, size: 256 }
     issuerRef: { name: temporal-ca, kind: ClusterIssuer, group: cert-manager.io }
   ```
4. **Issue the client certificates** from the same issuer. Each uses `usages: [client auth, digital signature, key encipherment]` and has no `dnsNames`.
   - **UI:** CN `temporal-ui`, Secret `temporal-web-client-tls` in the `temporal` namespace (2.8).
   - **Operator:** CN `operator`, Secret `temporal-operator-tls` in the `temporal` namespace. Copy it to your workstation with `kubectl get secret … -o jsonpath` (7.0).
   - **EKS Nexus Worker:** CN `eks-nexus-worker`, in the `temporal-workers` namespace (7b.2). cert-manager renews it automatically. That is a nice contrast with the manual Secrets Manager rotation on the AgentCore side.
   - **AgentCore Worker:** CN `agentcore-worker`, Secret `agentcore-worker-tls` in the `temporal` namespace. Give it a longer `duration` (for example `4320h`), because its copy in Secrets Manager is refreshed by hand.
   - *Resolved in v0.9: chart 1.7.0's admintools, namespace Job and helm test mount `temporal-frontend-tls` and present it as their **client** certificate, so with `requireClientAuth` the frontend certificate needs `usages: [server auth, client auth, ...]` (as in the chart's own CI values). See ADR-0007 v0.9.*
5. **Export the AgentCore Worker cert to Secrets Manager** (the format 5.3 expects). cert-manager cannot mount into AgentCore.
   ```bash
   kubectl get secret agentcore-worker-tls -n temporal -o json | jq -r '.data["tls.crt"]' | base64 -d > worker.crt
   kubectl get secret agentcore-worker-tls -n temporal -o json | jq -r '.data["tls.key"]' | base64 -d > worker.key
   kubectl get secret agentcore-worker-tls -n temporal -o json | jq -r '.data["ca.crt"]'  | base64 -d > ca.crt
   # then run the create-secret (first time) or put-secret-value (renewal) command in 5.3, and shred the local files
   ```
   Repeat after every renewal. Set a reminder from `renewBefore`, or run a small CronJob with IRSA that does the export.

- **Trade-off you accept.** The root CA private key lives in the Kubernetes Secret `cert-manager/temporal-root-ca`. Anyone who can read Secrets in `cert-manager` can mint client certs that Temporal trusts. Under authorization option (c), such a cert gets full access. Restrict RBAC on that namespace, and turn on EKS envelope encryption of Secrets with KMS. For production, the hardening path is an offline root with an in-cluster intermediate, or ACM Private CA.
- **Do not** generate the root with Terraform's `tls` provider, because that stores the private key in Terraform state.
- **Disaster recovery.** If the cluster is deleted, the root goes with it. Back up the `temporal-root-ca` Secret, or accept that you will re-issue every cert and update the Secrets Manager copy.
- ⚠️ Verify: whether the server hot-reloads rotated certs. Plan a rolling restart until you have confirmed it. *Resolved in v0.9 (Q10): yes, with `server.config.tls.refreshInterval: 1m` a re-issued frontend cert was served within 16–112 s, no restart.*

**2.7 Authorization — decide before Phase 7.** Without an Authorizer, the **noop** authorizer allows every call, including admin calls. The default ClaimMapper takes permissions **only from a JWT**. With `authorizer: default`, any caller that has only an mTLS cert gets `PermissionDenied`. That includes AgentCore Workers and CLI calls that carry no JWT. Choose one path:

| Option | What it means | What you must do |
|---|---|---|
| **(a) JWT for everyone** | `authorizer: default`, `claimMapper: default` (snippet below). mTLS stays on for transport. | Register an IdP client for Workers (client-credentials flow). Store the IdP client secret in Secrets Manager. The Worker fetches a JWT at startup and passes it as `api_key`/`TEMPORAL_API_KEY` in envconfig; the SDK sends it as `Authorization: Bearer`. ⚠️ Verify: refresh before expiry for sessions up to 8 h. Grant the Worker JWT a `agents:writer` permission claim. Grant operators a JWT with `temporal-system:admin`, or `agents:admin` for namespace-level work. |
| **(b) Custom ClaimMapper** | Map the client-cert subject to roles. | Requires a **custom server build** that registers your ClaimMapper. This is the most work. |
| **(c) First pass: noop authorizer** | Rely on mTLS (only certs from your CA are accepted) plus network isolation (SGs, internal NLB). | Leave `authorization` unset. **Record this as an accepted risk**: any holder of a valid client cert has admin access. |

Config for option (a):
```yaml
server:
  internal-frontend:
    enabled: true
  config:
    authorization:
      jwtKeyProvider:
        keySourceURIs:
          - https://idp.example.com/.well-known/jwks.json
        refreshInterval: 1m
      permissionsClaimName: permissions
      authorizer: default
      claimMapper: default
      audience: temporal-frontend
```
- With internal-frontend enabled, admintools and the namespace Job **bypass** the Authorizer. Their results can therefore differ from what a Worker sees. Set `useExternalFrontend: true` if you want them to go through it.
- Operator identity for Phase 7: under (a) the operator needs a JWT with admin permission, passed with `--api-key`. Under (c) the operator needs only an operator client cert.

**Decision for this demo: option (c), mTLS.** What that means with two namespaces:
- **Any** valid client cert from your CA can call **both** namespaces and use admin APIs. That includes creating or deleting Nexus Endpoints and namespaces. In particular, the AgentCore Worker's cert can poll `agent-services`, and the EKS Worker's cert can poll `agents`. **Record this as an accepted risk** for the demo.
- The caller Worker needs **no** access to `agent-services`. The server dispatches the Nexus call itself. Each Worker only needs its own namespace, which makes per-namespace scoping easy to express later.
- **Hardening path (option (b)).**
  1. Issue distinct CNs now: `agentcore-worker`, `eks-nexus-worker` and `operator`.
  2. Later, build a custom server binary that registers a ClaimMapper mapping cert subject → roles: `agentcore-worker` → `agents:writer`, `eks-nexus-worker` → `agent-services:writer`, `operator` → `temporal-system:admin`. Pair it with `authorizer: default`.

  ⚠️ Verify: how the default Authorizer evaluates Nexus endpoint dispatch, meaning which namespace and permission the caller's identity is checked against.
- Network isolation is part of the control. Only `sg-agentcore-worker`, the bastion and the cluster can reach 7233. The NetworkPolicy in 7b.6 limits which pods in the cluster can reach it.

**2.8 Web UI.**
- The chart sets `web.enabled: true` with `temporalio/ui:2.54.1`.
- Expose it through `web.ingress` (`className`, `hosts`, `tls`) as **`temporal-ui.temporal-customer-support.internal`**. Use an internal ALB (`alb.ingress.kubernetes.io/scheme: internal`), plus a Route 53 alias in the private zone. Unlike the frontend, the UI is plain HTTPS, so terminating TLS at the ALB is fine.
  - ACM cannot issue a public certificate for a `.internal` name. Either serve the UI over HTTP inside the VPC for the demo, or issue a cert-manager server cert for that name, import it into ACM, and trust the root CA on your browser machine.
  - The simplest option for the demo: skip the ingress and run `kubectl port-forward svc/temporal-web 8080:8080 -n temporal`.
- Configure SSO with the `TEMPORAL_AUTH_*` env vars through `web.additionalEnv` and `web.additionalEnvSecretName`.
- **Frontend mTLS does not configure the UI.** In chart 1.7.0 the UI gets `TEMPORAL_TLS_*` env only when `web.tls.enabled: true`, and the default is false. If you leave it off, the UI cannot connect to the frontend. Set:
```yaml
web:
  tls:
    enabled: true
    secretName: temporal-web-client-tls      # kubernetes.io/tls with ca.crt, tls.crt, tls.key (client cert needed because requireClientAuth: true)
    serverName: temporal-frontend.temporal-customer-support.internal
    enableHostVerification: true
```
The chart then sets `TEMPORAL_TLS_CA`, `TEMPORAL_TLS_CERT`, `TEMPORAL_TLS_KEY`, `TEMPORAL_TLS_SERVER_NAME` and `TEMPORAL_TLS_ENABLE_HOST_VERIFICATION`. Issue the UI client cert from the CA the frontend trusts.

**2.9 Metrics.**
```yaml
server:
  metrics:
    annotations:
      enabled: true
    serviceMonitor:
      enabled: true          # if Prometheus Operator
      interval: 30s
  config:
    metrics:
      prometheus:
        timerType: histogram
        listenAddress: "0.0.0.0:9090"
```
Import dashboards from https://github.com/temporalio/dashboards. 1.32 **removed `tasks_expired`**, so move any alerts on it to `tasks_dropped`.

**2.10 Namespaces.**
```yaml
server:
  config:
    namespaces:
      create: true
      namespace:
        - name: default
          retention: 3d
        - name: agents
          retention: 7d
        - name: agent-services
          retention: 7d
```
Or run it from the admintools pod:
```bash
kubectl exec -it deploy/temporal-admintools -n temporal -- temporal operator namespace create --namespace agents --retention 7d
kubectl exec -it deploy/temporal-admintools -n temporal -- temporal operator namespace create --namespace agent-services --retention 7d
```

**2.11 Install.**
1. **Snapshot first** if the RDS instance already holds data (Phase 11).
2. Dry-run:
   ```bash
   helm template temporal temporal --repo https://go.temporal.io/helm-charts --version 1.7.0 \
     -f values.postgresql.yaml -f my-overrides.yaml --namespace temporal \
     | kubectl apply --dry-run=server -n temporal -f -
   ```
3. Install:
   ```bash
   helm install --repo https://go.temporal.io/helm-charts --version 1.7.0 \
     -f values.postgresql.yaml -f my-overrides.yaml temporal temporal \
     --namespace temporal --create-namespace --timeout 900s
   ```
4. Create the Route 53 alias (2.4) if external-dns is not installed.
5. Check the shard count. admin-tools has no jq, so read the JSON by eye, or pipe it to jq on your workstation:
   ```bash
   kubectl exec deploy/temporal-admintools -n temporal -- temporal operator cluster describe -o json
   # workstation: ... | jq .historyShardCount
   ```

**2.12 1.32 behaviour changes to review before go-live.**
- The unified visibility query converter is now the default. It is stricter about types, and filtering a Text attribute against `""` now errors. The temporary revert is `system.visibilityEnableUnifiedQueryConverter: false`. The legacy converter is removed in 1.33.
- Activity eager execution is on by default. Standalone Activities are GA.
- `component.callbacks.allowedAddresses` is renamed to `callback.allowedAddresses`.
- The legacy Worker Versioning APIs are disabled and will be removed in 1.33. `matching.enableWorkerDeploymentVersionDemotionSignal` defaults to false.
- Upgrades go one minor version at a time, schema before binaries. Example path: 1.30.8 → 1.31.4 → 1.32.1.

---

### Phase 3 — Enable Serverless Workers (WCI) on the server

Put these keys in `server.dynamicConfig` in `my-overrides.yaml` **before the first install**. **Append** to the lists if they already contain values such as `aws-lambda`.

```yaml
server:
  dynamicConfig:
    workercontroller.enabled:
      - value: true
        constraints:
          namespace: 'agents'          # keep this: the WCI must NOT manage agent-services
    workercontroller.compute_providers.enabled:
      - value:
          - aws-agentcore
    workercontroller.scaling_algorithms.enabled:
      - value:
          - no-sync
    # Only for the standalone-Activity Nexus variant (7b.4), ⚠️ verify support on 1.32.1:
    # activity.enableCallbacks:
    #   - value: true
    #     constraints:
    #       namespace: 'agent-services'
```
- **Nexus needs nothing here on 1.32.** Do **not** add `system.enableNexus`, which was removed in 1.31. Do **not** add `component.nexusoperations.useSystemCallbackURL`, which was removed in 1.32. You do not need `callback.allowedAddresses` either, because `temporal://system` is always allowed.
- Leave `workercontroller.compute_providers.aws.require_role_and_external_id` at its default of **true**.
- **Reloading.** A `helm upgrade` that changes only `server.dynamicConfig` updates the `<release>-dynamic-config` ConfigMap **without** restarting pods:
  - The Deployment's `checksum/config` covers only `server-configmap.yaml`.
  - Kubelet syncs the mounted file, usually within about a minute.
  - The server polls the file every 10 s.

  Changes to `server.config`, images or env vars **do** trigger a rolling restart. Always make dynamic config changes in the values file. A `kubectl` patch is lost on the next `helm upgrade`.
- Add the Region:
  ```yaml
  server:
    additionalEnv:
      - name: AWS_REGION
        value: us-east-1
  ```
  The WCI also derives the Region from the endpoint ARN.
- **Verify:**
  ```bash
  kubectl get cm temporal-dynamic-config -n temporal -o yaml | grep -A3 workercontroller
  kubectl logs deploy/temporal-worker -n temporal | grep -i workercontroller
  ```
  ⚠️ Verify: the exact log lines the WCI component prints at startup. *Resolved in v0.9: no dedicated line; the worker service logs `dynamic config changed for the key: workercontroller.enabled ... constraints: {{Namespace:agents}}` and starts a per-namespace system Worker on `temporal-sys-per-ns-tq`. A dynamic-config-only upgrade applied in 10–67 s with 0 restarts.*

---

### Phase 4 — IAM: server identity, invocation role, execution role

There are three **separate** roles. Do not merge them.

| Role | Assumed by | Purpose |
|---|---|---|
| **Server role** (`temporal-server-wci`) | Temporal **worker-service** pods, via IRSA (SA `temporal-worker`) | May only `sts:AssumeRole` the invocation role |
| **Invocation role** (`Temporal-AgentCore-Worker`) | The WCI, with the External ID | `InvokeAgentRuntime` and `GetAgentRuntimeEndpoint` |
| **Runtime execution role** | `bedrock-agentcore.amazonaws.com` | What the Worker code runs as: Logs, X-Ray, Bedrock, Secrets Manager |

(If you chose OpenSearch or IAM DB auth in 2.2, a fourth role on the global ServiceAccount holds `es:ESHttp*` / `rds-db:connect`.)

**4.1 Server role (IRSA). Decided: IRSA, not Pod Identity.**
- IRSA adds no session tags, so Temporal's self-hosted invocation-role template (Phase 6) works **unchanged**.
- Pod Identity's transitive session tags would probably need `sts:TagSession` on that trust policy. That is unverified, and it would mean editing Temporal's template.
- Pod Identity's benefits (no per-cluster OIDC provider, roles reusable across clusters) don't matter for one demo cluster.
- The EKS Nexus Workers also use IRSA (7b.3), so the cluster has one identity mechanism.
- **Trust:** Federated = your EKS OIDC provider, Action `sts:AssumeRoleWithWebIdentity`, with these conditions:
  - `<oidc>:aud = sts.amazonaws.com`
  - `<oidc>:sub = system:serviceaccount:temporal:temporal-worker`
- **Permissions:**
  ```json
  {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"sts:AssumeRole",
    "Resource":"arn:aws:iam::<AGENTCORE_ACCOUNT>:role/Temporal-AgentCore-Worker"}]}
  ```
- **Attach the role through Helm, scoped to the worker service only.** In chart 1.7.0, `server.<svc>.serviceAccount` supports `create`, `name` and `extraAnnotations`. Components you leave unset fall back to the global `serviceAccount`. Keep the global SA **without** the IRSA annotation.
  ```yaml
  server:
    worker:
      serviceAccount:
        create: true
        name: temporal-worker
        extraAnnotations:
          eks.amazonaws.com/role-arn: arn:aws:iam::<ACCOUNT>:role/temporal-server-wci
  ```
  Simpler fallback: annotate the global `serviceAccount` so every pod gets the role, and set the trust `sub` to that SA. The blast radius is small, because the role can only assume the invocation role.
- **Pod Identity alternative** (for reference only; not used in this design):
  ```bash
  aws eks create-pod-identity-association --cluster-name <c> --namespace temporal \
    --service-account temporal-worker --role-arn <role-arn>
  ```
  - EKS docs confirm that Pod Identity session tags are **transitive**: they pass to any AssumeRole call.
  - Whether the invocation-role trust policy must then allow `sts:TagSession` is UNVERIFIED. Test it.
  - Workarounds: turn on "Disable session tags" on the Pod Identity association, or stay on IRSA.
- **The ARN for CloudFormation** is the IAM role you created here: `arn:aws:iam::<acct>:role/temporal-server-wci`. It is already in `iam` form. Never pass an `assumed-role` session ARN.
- **Check that IRSA is wired.** The server image has no `aws` CLI.
  ```bash
  kubectl get sa temporal-worker -n temporal -o yaml | grep role-arn
  kubectl exec deploy/temporal-worker -n temporal -- env | grep -E 'AWS_ROLE_ARN|AWS_WEB_IDENTITY_TOKEN_FILE'
  # Optional live check with a throwaway pod using the same SA:
  kubectl run awscli --rm -it -n temporal --image=amazon/aws-cli \
    --overrides='{"spec":{"serviceAccountName":"temporal-worker"}}' -- sts get-caller-identity
  ```

**4.2 Runtime execution role.** The AgentCore CLI/CDK creates this role automatically. **Add** a statement for your secrets, plus `kms:Decrypt` on the CMK if you use one:
```json
{"Effect":"Allow","Action":"secretsmanager:GetSecretValue",
 "Resource":"arn:aws:secretsmanager:us-east-1:<acct>:secret:temporal/agentcore-worker-mtls-*"}
```
- Under authorization option (a), also allow the IdP client-secret ARN.
- Its trust is the service `bedrock-agentcore.amazonaws.com`, with conditions `aws:SourceAccount` and `aws:SourceArn` = `arn:aws:bedrock-agentcore:us-east-1:<acct>:*`.
- The base policy covers Logs, X-Ray, CloudWatch (`bedrock-agentcore` namespace), workload access tokens and `bedrock:InvokeModel*`. Narrow `bedrock:InvokeModel*` to the `global.anthropic.claude-opus-5-5` inference profile and the `anthropic.claude-opus-5-5` foundation model in every Region (3.2).
- ⚠️ Verify: the IAM actions for Code Interpreter or other tools, if your Activities use them.

**4.3 Invocation role.** Create it in Phase 6, after the Runtime exists, because it needs the Runtime ARN.

---

### Phase 5 — Build the Python Worker and deploy it to AgentCore Runtime

**5.1 Start from a reference implementation.**
- Minimal Worker: https://github.com/temporalio/documentation-sdk-code-examples/blob/main/python-sandbox/workers/agentcore_worker/worker.py
- End-to-end Strands agent:
  ```bash
  git clone --depth 1 https://github.com/temporalio/samples-python.git
  cd samples-python/bedrock_agentcore/strands_agent
  uv sync
  ```
  This sample needs `temporalio>=1.34.0` because of `temporalio-strands-agents`. Its `bin/create-runtime.sh` and the docs' `agentcore` commands target an older AgentCore CLI (see 5.5).

**5.2 Worker requirements** (from the Temporal Python AgentCore docs):
- Dependencies in `pyproject.toml` inside `codeLocation`:
  - `temporalio`
  - `bedrock-agentcore` (the sample pins ≥1.9.1)
  - `boto3`, for the Secrets Manager fetch

  ⚠️ Verify: the minimum `temporalio` version for a plain Worker without Strands. **Use ≥1.34.0** to match the sample. *v0.7: this Worker needs 1.34.0 (plugin); the versioning APIs exist since Python SDK 1.11 (docs); the plain-Worker minimum is asked of Temporal (`docs/ask-temporal.md`).*
- Worker versioning:
  `deployment_config=WorkerDeploymentConfig(version=WorkerDeploymentVersion(deployment_name, build_id), use_worker_versioning=True, default_versioning_behavior=VersioningBehavior.PINNED)`.
- Entrypoint: `app = BedrockAgentCoreApp()`. In `@app.entrypoint`:
  - guard against starting a second Worker in the same session;
  - call `task_id = app.add_async_task("temporal-worker")`;
  - start the Worker with `asyncio.create_task(...)`;
  - return an acknowledgment right away.
- Idle and drain policy:
  - Use an `ActivityTracker`-style interceptor that exits after `AGENTCORE_DEBOUNCE_SECONDS` with no Activity starting or finishing.
  - Shut down with `graceful_shutdown_timeout` (120 s in the sample).
  - Call **`app.complete_async_task(task_id)` in a `finally` block**. Without it, the session stays HealthyBusy for up to 8 h.
- `BedrockAgentCoreApp` serves `/invocations` and `/ping` on 0.0.0.0:8080. Container builds must be **ARM64**; CodeZip handles this for you.
- Long Activities must use **heartbeats and timeouts**, because a session cannot outlive `maxLifetime`.

**5.3 Change the connection code for self-hosted mTLS.** The sample uses `api_key=…` and `tls=bool(api_key)`, which works only for Temporal Cloud.

- **Create the mTLS secret.** The name must match the `temporal/agentcore-worker-mtls-*` resource in 4.2:
  ```bash
  aws secretsmanager create-secret --name temporal/agentcore-worker-mtls \
    --kms-key-id <cmk-arn> \
    --secret-string "$(jq -n --rawfile c worker.crt --rawfile k worker.key --rawfile a ca.crt \
        '{tls_crt:$c, tls_key:$k, ca_crt:$a}')"
  ```
- Load the client config through **`temporalio.envconfig`**, using these env vars: `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TLS=true`, `TEMPORAL_TLS_SERVER_NAME`, `TEMPORAL_TLS_CLIENT_CERT_DATA`, `TEMPORAL_TLS_CLIENT_KEY_DATA` and `TEMPORAL_TLS_SERVER_CA_CERT_DATA`.
- Fetch the cert, key and CA **from Secrets Manager at startup**, before the client connects. Do not put them in `agentcore.json`. Sample:
  ```python
  import json, os, boto3
  from temporalio.client import Client
  from temporalio.envconfig import ClientConfig

  def load_mtls_into_env() -> None:
      sm = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
      s = json.loads(sm.get_secret_value(SecretId=os.environ["TEMPORAL_MTLS_SECRET_ID"])["SecretString"])
      os.environ["TEMPORAL_TLS_CLIENT_CERT_DATA"] = s["tls_crt"]
      os.environ["TEMPORAL_TLS_CLIENT_KEY_DATA"] = s["tls_key"]
      os.environ["TEMPORAL_TLS_SERVER_CA_CERT_DATA"] = s["ca_crt"]
      # Option (a) only: fetch a JWT from your IdP here and set os.environ["TEMPORAL_API_KEY"]

  async def connect() -> Client:
      load_mtls_into_env()
      return await Client.connect(**ClientConfig.load_client_connect_config())
  ```
  ⚠️ Verify: the `ClientConfig.load_client_connect_config()` name against your `temporalio` version.
- **Cert lifetime and rotation.** Each session fetches the secret at startup, so sessions (up to 8 h) keep the cert they started with. To rotate:
  1. Issue the new cert while the old one is still valid, with at least 8 h of overlap.
  2. Update the secret with `aws secretsmanager put-secret-value`.
  3. New sessions pick up the new cert.
  4. Revoke or let the old cert expire only after `maxLifetime` has passed.
- ⚠️ Verify: no documented Temporal pattern covers mTLS from AgentCore. A third-party source also says AgentCore cannot inject secrets as env vars natively.

**5.4 Configure `agentcore/agentcore.json`.** This is the runtime definition, based on the sample and the AgentCore CLI schema (`src/projectSchemas/runtime.ts`). The runtime entry looks like this:
```json
{
  "name": "<RUNTIME_NAME>",
  "build": "CodeZip",
  "entrypoint": "<your_worker>.py",
  "codeLocation": ".",
  "runtimeVersion": "PYTHON_3_12",
  "protocol": "HTTP",
  "authorizerType": "AWS_IAM",
  "networkMode": "VPC",
  "networkConfig": {
    "subnets": ["subnet-aaa", "subnet-bbb"],
    "securityGroups": ["sg-agentcore-worker"]
  },
  "endpoints": { "temporal_v1": { "version": 1 } },
  "lifecycleConfiguration": { "idleRuntimeSessionTimeout": 900, "maxLifetime": 28800 },
  "envVars": [
    {"name": "TEMPORAL_ADDRESS",          "value": "temporal-frontend.temporal-customer-support.internal:7233"},
    {"name": "TEMPORAL_NAMESPACE",        "value": "agents"},
    {"name": "TEMPORAL_TASK_QUEUE",       "value": "<TASK_QUEUE>"},
    {"name": "TEMPORAL_DEPLOYMENT_NAME",  "value": "<DEPLOYMENT_NAME>"},
    {"name": "TEMPORAL_BUILD_ID",         "value": "<BUILD_ID>"},
    {"name": "AWS_REGION",                "value": "us-east-1"},
    {"name": "AGENTCORE_DEBOUNCE_SECONDS","value": "60"},
    {"name": "BEDROCK_MODEL_ID",          "value": "global.anthropic.claude-opus-5-5"},
    {"name": "TEMPORAL_TLS",              "value": "true"},
    {"name": "TEMPORAL_TLS_SERVER_NAME",  "value": "temporal-frontend.temporal-customer-support.internal"},
    {"name": "TEMPORAL_MTLS_SECRET_ID",   "value": "temporal/agentcore-worker-mtls"}
  ]
}
```
- `authorizerType: AWS_IAM` is required, so that the WCI's SigV4 calls are accepted.
- `networkMode: "VPC"` requires `networkConfig`, and `networkConfig` is rejected in PUBLIC mode. `vpcId` is required only for `build: "Container"` in VPC mode; it is not needed for CodeZip. The sample uses PUBLIC.
- **Endpoint names allow no hyphens.** They must match `[a-zA-Z][a-zA-Z0-9_]{0,47}`. Temporal's conceptual page uses `temporal-v1` as its example, which fails validation.
- `envVars` is an array of `{name, value}` objects, not `KEY=VALUE` strings.
- CLI equivalent: `agentcore add runtime --network-mode VPC --network-config '<json>' --lifecycle-configuration '<json>' …`.
- Set the account and Region in `agentcore/aws-targets.json`.

**Fallback if the CLI cannot produce a VPC runtime.** Deploy the artifact with `agentcore deploy`, then create or update the runtime and named endpoint directly. The command below is a **fragment for illustration**. A runnable call needs `--agent-runtime-artifact` and `--role-arn` as well as the name.
```bash
aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name "<RUNTIME_NAME>" \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ecr-uri>"}}' \
  --role-arn <execution-role-arn> \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{"subnets":["subnet-aaa","subnet-bbb"],"securityGroups":["sg-agentcore-worker"]}}' \
  --lifecycle-configuration '{"idleRuntimeSessionTimeout":900,"maxLifetime":28800}' \
  --environment-variables '{"TEMPORAL_ADDRESS":"…", "…":"…"}'
# (or a codeConfiguration artifact instead of containerConfiguration; also --protocol-configuration as needed)
aws bedrock-agentcore-control create-agent-runtime-endpoint --agent-runtime-id <id> \
  --name temporal_v1 --agent-runtime-version <N>
```
CloudFormation and CDK alternatives:
- `AWS::BedrockAgentCore::Runtime` (`NetworkConfiguration.NetworkMode` / `NetworkModeConfig`) and `AWS::BedrockAgentCore::RuntimeEndpoint`. ⚠️ Verify: the RuntimeEndpoint resource name.
- CDK: `aws_cdk.aws_bedrockagentcore`.
- ⚠️ Verify: Terraform resource names.

**5.5 Deploy and record the ARNs** (AgentCore CLI 1.0.x; check `agentcore --version` and `agentcore <cmd> --help` first):
```bash
agentcore build                                  # validates/builds (no `validate` command in 1.0.x)
agentcore deploy --target default
agentcore status --target default --json         # Runtime ARN / ID
aws bedrock-agentcore-control list-agent-runtime-endpoints --agent-runtime-id <RUNTIME_ID> \
  --query "runtimeEndpoints[?name=='temporal_v1'].agentRuntimeEndpointArn | [0]" --output text
# or: agentcore runtime endpoint get --id <RUNTIME_ID> --qualifier temporal_v1
```
Alternatively, pin the older `@aws/agentcore` version the sample was written for.

**Check VPC mode:**
```bash
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <RUNTIME_ID> \
  --query 'networkConfiguration'
```
It should show `networkMode: VPC` with the expected subnets and SGs.

Where the ARNs go:
- Runtime ARN: `arn:aws:bedrock-agentcore:us-east-1:<acct>:runtime/<runtime-id>`. It goes into the **IAM policy, with a trailing `*`**.
- Endpoint ARN: `<runtime ARN>/runtime-endpoint/temporal_v1`. It goes to **Temporal**. **Never** pass the bare Runtime ARN or the `DEFAULT` endpoint.

**5.6 Support Supervisor Workflow: a Strands supervisor with sub-agents as tools, and an A2A call to Finance.**

The code below is a design sketch, not a tested implementation. It follows the Temporal Strands plugin API (`TemporalAgent`, `activity_as_tool`, `StrandsPlugin`, which is Public Preview) and the Python Nexus API.

1. **Shared A2A contract.** Put it in a small shared package, `finance_a2a_api/`. Vendor it into **both** the AgentCore CodeZip (`codeLocation`) and the EKS Worker image. It is the *only* code the two teams share, which is the point.
   ```python
   # finance_a2a_api/a2a.py   (A2A-shaped types; see §1.4)
   from dataclasses import dataclass, field
   from typing import Any, Literal
   import nexusrpc

   TaskState = Literal["submitted", "working", "input-required", "completed", "rejected", "failed", "canceled"]

   @dataclass
   class Part:
       kind: Literal["text", "data"]
       text: str | None = None
       data: dict[str, Any] | None = None

   @dataclass
   class A2AMessage:
       role: Literal["user", "agent"]
       parts: list[Part]
       message_id: str
       context_id: str                 # the Support Workflow ID; correlates the conversation
       task_id: str | None = None

   @dataclass
   class TaskStatus:
       state: TaskState
       message: A2AMessage | None = None

   @dataclass
   class Artifact:
       name: str
       parts: list[Part]

   @dataclass
   class A2ATask:
       id: str
       context_id: str
       status: TaskStatus
       artifacts: list[Artifact] = field(default_factory=list)

   @dataclass
   class AgentSkill:
       id: str
       name: str
       description: str

   @dataclass
   class AgentCard:
       name: str
       description: str
       version: str
       skills: list[AgentSkill]

   @dataclass
   class AgentCardRequest:
       pass

   @nexusrpc.service
   class FinanceAgentA2A:
       get_agent_card: nexusrpc.Operation[AgentCardRequest, AgentCard]   # sync: discovery
       send_message: nexusrpc.Operation[A2AMessage, A2ATask]             # async: the A2A task
   ```
2. **Support Supervisor Workflow**, registered on the AgentCore Worker in `agents`:
   ```python
   # support/workflows.py
   from datetime import timedelta
   from temporalio import workflow
   from temporalio.strands_agents import TemporalAgent
   from temporalio.strands_agents.workflow import activity_as_tool
   with workflow.unsafe.imports_passed_through():
       from strands import tool
       from finance_a2a_api.a2a import A2AMessage, AgentCardRequest, FinanceAgentA2A, Part
       from support.activities import lookup_order, get_return_policy

   NEXUS_ENDPOINT = "finance-agent"
   MODEL_TIMEOUT = timedelta(seconds=60)

   SUPERVISOR_PROMPT = """You are the Customer Support supervisor.
   Use order_lookup_agent and return_policy_agent to establish the facts.
   You CANNOT issue refunds. For any refund, call ask_finance_agent with the order facts and wait
   for the Finance agent's decision. Then write a short, friendly reply to the customer."""

   @workflow.defn
   class SupportSupervisorWorkflow:
       def __init__(self) -> None:
           self.finance = workflow.create_nexus_client(service=FinanceAgentA2A, endpoint=NEXUS_ENDPOINT)

           # Sub-agents owned by Support, used as tools ("agents as tools"); in-process, same Workflow
           order_agent = TemporalAgent(
               system_prompt="You look up orders. Answer with order id, item, price (USD), delivery date.",
               tools=[activity_as_tool(lookup_order, start_to_close_timeout=timedelta(seconds=10))],
               model="bedrock", start_to_close_timeout=MODEL_TIMEOUT)
           policy_agent = TemporalAgent(
               system_prompt="You decide refund eligibility from the return policy. Answer ELIGIBLE or NOT_ELIGIBLE with a reason.",
               tools=[activity_as_tool(get_return_policy, start_to_close_timeout=timedelta(seconds=10))],
               model="bedrock", start_to_close_timeout=MODEL_TIMEOUT)

           @tool
           async def order_lookup_agent(question: str) -> str:
               """Ask Support's Order Lookup agent about an order."""
               return str(await order_agent.invoke_async(question))

           @tool
           async def return_policy_agent(question: str) -> str:
               """Ask Support's Return Policy agent whether a refund is eligible."""
               return str(await policy_agent.invoke_async(question))

           @tool
           async def ask_finance_agent(order_id: str, amount_usd: float, reason: str, summary: str) -> str:
               """A2A: ask the Finance team's agent to approve and issue a refund. May take hours (human approval)."""
               msg = A2AMessage(
                   role="user", message_id=str(workflow.uuid4()), context_id=workflow.info().workflow_id,
                   parts=[Part(kind="text", text=summary),
                          Part(kind="data", data={"order_id": order_id, "amount_usd": amount_usd, "reason": reason})])
               task = await self.finance.execute_operation(            # ← the A2A hop (Nexus, cross-namespace)
                   FinanceAgentA2A.send_message, msg, schedule_to_close_timeout=timedelta(hours=24))  # v0.6: 25h (ADR-0009)
               reply = task.status.message.parts[0].text if task.status.message else ""
               return f"Finance agent task {task.id} is {task.status.state}: {reply}"

           self.supervisor = TemporalAgent(
               system_prompt=SUPERVISOR_PROMPT,
               tools=[order_lookup_agent, return_policy_agent, ask_finance_agent],
               model="bedrock", start_to_close_timeout=MODEL_TIMEOUT)

       @workflow.run
       async def run(self, ticket: str) -> str:
           # A2A discovery: fetch Finance's Agent Card (a sync Nexus op) so the hop is visible from the first step
           card = await self.finance.execute_operation(FinanceAgentA2A.get_agent_card, AgentCardRequest())
           workflow.logger.info("A2A peer discovered: %s v%s skills=%s", card.name, card.version, [s.id for s in card.skills])
           return str(await self.supervisor.invoke_async(f"Customer ticket: {ticket}"))
   ```
3. **Activities** (`support/activities.py`): `lookup_order` and `get_return_policy` read mock data bundled in the CodeZip. In the demo they need no AWS calls apart from Bedrock, which the plugin's model Activity makes using the AgentCore execution role.
4. **Worker wiring.** Connect the AgentCore Worker's client with `plugins=[StrandsPlugin(models={"bedrock": lambda: BedrockModel(model_id=os.environ["BEDROCK_MODEL_ID"], region_name=os.environ["AWS_REGION"])})]`. The Strands AgentCore sample uses a bare `StrandsPlugin()`, which would run Strands' default model instead of Opus 5.5. Every `TemporalAgent` above sets `model="bedrock"`; with a custom mapping, a missing or unknown model name raises `ValueError` inside the model Activity. Register `SupportSupervisorWorkflow`, `lookup_order` and `get_return_policy`. There are **no** `nexus_service_handlers`, because Support only *calls* Finance.
5. **Notes and ⚠️ Verify items:**
   - **Where the A2A tool runs.** The Strands plugin runs plain Strands `@tool` functions **in Workflow context**. Activity-backed tools use `activity_as_tool`. `ask_finance_agent` must stay a plain `@tool` so that it can call the Workflow-only Nexus client.
     - ⚠️ Verify that the plugin awaits `async` in-workflow tools on the Workflow event loop. *Resolved in v0.5 (ADR-0003 part 2): yes; `ask_finance_agent` awaits `send_message` inside the Supervisor's turn. The tool must be `async def`: Strands runs a sync tool with `asyncio.to_thread`, which fails inside a Workflow.*
     - ⚠️ Verify that Strands applies no tool-execution timeout that would cut off a wait of several hours. *v0.5: no timeout in Strands' tool executors (source); the multi-hour wait is tested in v0.6.* *Resolved in v0.6: no timeout; a 31-minute real wait with the Support Worker stopped completes after `make approve` (ledger 18). The caller's budget is 25 h, not 24 h (ADR-0009).*
     - **Fallback (an explicit handoff).** Give the Supervisor a `structured_output_model=RefundRequest` and let it stop. *Built in v0.5 as `SUPPORT_A2A_MODE=handoff`, with an unforced capture tool `submit_refund_request` instead of `structured_output_model` (ADR-0001 §6).* Make the Nexus call in plain Workflow code. Then call `self.supervisor.invoke_async(...)` again with Finance's reply. The A2A hop is just as visible this way, arguably more so.
   - **Agents as tools.** Calling another `TemporalAgent.invoke_async` from inside an in-workflow `@tool` is a nested agent loop. Each sub-agent's model calls show up as their own `invoke_model` Activities. ⚠️ Verify nesting with the plugin version you use. If it misbehaves, run each sub-agent as a separate step in `run()`. *Resolved in v0.3 (ADR-0003): nesting works with a hand-written `@tool` that builds a fresh `TemporalAgent` per call. Avoid Strands' `Agent.as_tool()` in a Workflow, because its cancel-signal poll writes durable timers. Avoid one shared sub-agent instance, because parallel calls raise `ConcurrencyException`.*
   - **Session draining.** The debounce interceptor (5.2) watches Activities. While the Supervisor waits on Finance, no Activities run, so the AgentCore session drains after `AGENTCORE_DEBOUNCE_SECONDS`. That is the intended Case B behaviour (§1.3).
   - Add `nexus-rpc` and `temporalio-strands-agents` to the AgentCore `pyproject.toml`; the plugin needs `temporalio>=1.34.0`. ⚠️ Verify with `uv tree`.

---

### Phase 6 — Create the invocation role (self-hosted CloudFormation template)

**6.1 Generate and store the External ID first.**
```bash
EXTERNAL_ID=$(openssl rand -hex 16)      # 32 chars, within [a-zA-Z0-9_+=,.@-], length 5–45
aws secretsmanager create-secret --name temporal/agentcore-external-id --secret-string "$EXTERNAL_ID"
```

**6.2 Download the template.** Use the "Download the CloudFormation template" link on the self-hosted setup page (https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/agentcore/self-hosted-setup#create-invocation-role). At the time of writing it pointed to:
```bash
curl -o temporal-self-hosted-serverless-worker-agentcore-role.yaml \
  https://docs.temporal.io/assets/files/temporal-self-hosted-serverless-worker-agentcore-role-d633a078183a821913145e7fb41bb8cb.yaml
```
The hash in the file name can change. The older `/files/…` URL returns 404. **Do not** use the Cloud template or the sample's `mk-invoke-role.sh`, because both trust Temporal Cloud. Open the file and confirm the parameter names before you deploy. The expected parameters are `TemporalIamRoleArn`, `AssumeRoleExternalId`, `AgentRuntimeARNs` and `RoleName`, and the expected output is `RoleARN`.

**6.3 Create the stack.**
```bash
aws cloudformation create-stack \
  --stack-name temporal-agentcore-worker \
  --template-body file://temporal-self-hosted-serverless-worker-agentcore-role.yaml \
  --parameters \
    ParameterKey=TemporalIamRoleArn,ParameterValue=arn:aws:iam::<EKS_ACCT>:role/temporal-server-wci \
    ParameterKey=AssumeRoleExternalId,ParameterValue=$EXTERNAL_ID \
    ParameterKey=AgentRuntimeARNs,ParameterValue='<AGENT_RUNTIME_ARN>*' \
  --capabilities CAPABILITY_NAMED_IAM --region us-east-1

aws cloudformation wait stack-create-complete --stack-name temporal-agentcore-worker --region us-east-1
aws cloudformation describe-stacks --stack-name temporal-agentcore-worker \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleARN`].OutputValue' --output text --region us-east-1
```
- `RoleName` defaults to `Temporal-AgentCore-Worker` and has a 64-character limit.
- Trust: `sts:AssumeRole` from `TemporalIamRoleArn`, with the condition `sts:ExternalId`. `MaxSessionDuration` is 3600.
- The WCI **checks that the External ID is enforced**. It tries AssumeRole *without* the External ID, and fails the version if that call succeeds.
- Cross-account setups work as written. Do **not** use the Pod Identity "target role" feature for this role.
- AWS Organizations RCPs and SCPs can silently block `sts:AssumeRole`.
- **Rotating the External ID:**
  1. Generate a new ID and update the secret.
  2. Run `aws cloudformation update-stack … ParameterKey=AssumeRoleExternalId,ParameterValue=<new>`.
  3. For each Worker Deployment Version, run `temporal worker deployment update-version-compute-config … --aws-agentcore-assume-role-external-id <new>`.

  Invocations fail between steps 2 and 3, so rotate in a quiet window. ⚠️ Verify: whether the template can accept two IDs during the changeover.

---

### Phase 7 — Register the Worker Deployment Version with Temporal

**7.0 Operator access.** The frontend sits behind an internal NLB and requires client certs. Choose one way in:
- **Port-forward** from a workstation that has cluster access:
  ```bash
  kubectl port-forward svc/temporal-frontend 7233:7233 -n temporal
  export TEMPORAL_ADDRESS=localhost:7233
  export TEMPORAL_TLS_SERVER_NAME=temporal-frontend.temporal-customer-support.internal
  export TEMPORAL_TLS_CLIENT_CERT_PATH=operator.crt TEMPORAL_TLS_CLIENT_KEY_PATH=operator.key TEMPORAL_TLS_SERVER_CA_CERT_PATH=ca.crt
  # option (a): export TEMPORAL_API_KEY=<admin JWT>
  ```
- Or reach the VPC through SSM Session Manager or a VPN to a bastion, and use `--address temporal-frontend.temporal-customer-support.internal:7233`.
- The **operator client cert** is `temporal-operator-tls`, from the `temporal-ca` ClusterIssuer (2.6). Pull it locally:
  ```bash
  for k in tls.crt tls.key ca.crt; do
    kubectl get secret temporal-operator-tls -n temporal -o json | jq -r ".data[\"$k\"]" | base64 -d > "${k/tls/operator}"
  done   # writes operator.crt, operator.key, ca.crt
  ```
  Under option (a), also get a JWT with admin permission.
- The admintools pod goes through internal-frontend and **bypasses the Authorizer**. Use it for break-glass, not to check what a Worker would see.

**7.1 Prerequisites.**
```bash
temporal --version                                 # ≥ 1.8.3
temporal operator namespace describe --namespace agents   # must succeed
temporal worker deployment --help                  # confirm create / create-version / set-current-version exist
```
If `create` is absent, `create-version` may create the deployment implicitly (⚠️ Verify). *Resolved in v0.7 (dev server 1.32.0): `create` exists and is required; `create-version` on an uncreated deployment fails. `create-version` also invokes the runtime once and fails if it cannot be reached.*

**7.2 Register.**
```bash
temporal worker deployment create --namespace agents --name <DEPLOYMENT_NAME>

temporal worker deployment create-version \
  --namespace agents \
  --deployment-name <DEPLOYMENT_NAME> \
  --build-id <BUILD_ID> \
  --aws-agentcore-endpoint-arn <RUNTIME_ENDPOINT_ARN> \
  --aws-agentcore-assume-role-arn <INVOCATION_ROLE_ARN> \
  --aws-agentcore-assume-role-external-id "$EXTERNAL_ID"

# Run IMMEDIATELY after create-version (the Worker exits ~AGENTCORE_DEBOUNCE_SECONDS after going idle)
temporal worker deployment set-current-version \
  --namespace agents --deployment-name <DEPLOYMENT_NAME> --build-id <BUILD_ID> --yes
# If it fails with a no-pollers error, re-run with --allow-no-pollers
```
- `<DEPLOYMENT_NAME>` and `<BUILD_ID>` **must equal** `TEMPORAL_DEPLOYMENT_NAME` and `TEMPORAL_BUILD_ID` in the Runtime env.
- `create-version` invokes the Runtime and waits for the Worker to register. If it hangs or fails, go to §6.
- The AgentCore docs use `set-current-version` without `--allow-no-pollers`. Temporal's Lambda samples pass it for the same no-long-lived-poller reason. It is UNVERIFIED whether AgentCore needs it. *v0.7 (dev server, informational): not needed for a version with a compute config, even with no Worker polling; definitive at v0.12.*
- To change the endpoint, role or External ID later, run `temporal worker deployment update-version-compute-config` with the same `--aws-agentcore-*` flags.
- Do not use `--aws-agentcore-skip-role-and-external-id`. It only works with `require_role_and_external_id: false`, which is not recommended.

---

### Phase 7b — Nexus handler on EKS and the Nexus Endpoint

You can do this phase in parallel with Phases 5–7. It must be finished before Phase 8 step 7.

**7b.1 Prerequisites.**
- The `agent-services` namespace exists (2.10).
- The frontend cert has the in-cluster SANs (2.5).
- cert-manager and the `temporal-ca` ClusterIssuer exist (2.6).
- An ECR repository exists for the handler image, for example `agent-services-worker`. Build it for your **node** architecture. Unlike AgentCore, this does not have to be ARM64.

**7b.2 Client certificate (cert-manager).**
```yaml
apiVersion: v1
kind: Namespace
metadata: { name: temporal-workers }
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: { name: eks-nexus-worker-tls, namespace: temporal-workers }
spec:
  secretName: eks-nexus-worker-tls          # kubernetes.io/tls with tls.crt, tls.key, ca.crt
  commonName: eks-nexus-worker
  duration: 720h
  renewBefore: 168h
  usages: [client auth, digital signature, key encipherment]
  privateKey: { algorithm: ECDSA, size: 256, rotationPolicy: Always }
  issuerRef: { name: temporal-ca, kind: ClusterIssuer, group: cert-manager.io }
```
- The `CA` issuer writes the root into `ca.crt`, so `TEMPORAL_TLS_SERVER_CA_CERT_PATH=/etc/temporal/tls/ca.crt` trusts the frontend cert from 2.6.
- **Rotation.** cert-manager rewrites the Secret, and the kubelet updates the mounted files. The Python SDK reads the cert at connect time. Restart the Deployment after renewal (for example with Reloader or a `kubectl rollout restart` CronJob), or reconnect in code. ⚠️ Verify whether the SDK reloads file-based certs. *Resolved in v0.9 (Q18): it does not; the files are read once at connect. Restart after renewal (the old cert stays valid until it expires).*

**7b.3 ServiceAccount and IRSA.** The Finance agent calls Bedrock for its model turns, so it needs this. (A handler that only computes could skip IRSA and use a plain ServiceAccount.) Create the role `agent-services-worker` and give it an IRSA trust like 4.1, with `sub = system:serviceaccount:temporal-workers:agent-services-worker`. Scope its permissions to what the handler needs: `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the `global.anthropic.claude-opus-5-5` inference profile, and on the `anthropic.claude-opus-5-5` foundation model in every Region (see 3.2). **Do not** reuse `temporal-server-wci`.
```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-services-worker
  namespace: temporal-workers
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<ACCOUNT>:role/agent-services-worker   # omit if no AWS calls
```

**7b.4 Handler code.**

This is a design sketch of the **Finance Refund agent**: an A2A peer owned by the Finance team. It is a separate Strands agent in its own Workflow and namespace.

*Nexus service handler: the A2A "server".*
```python
# finance/service_handler.py
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio import nexus
from finance_a2a_api.a2a import A2AMessage, A2ATask, AgentCard, AgentCardRequest, AgentSkill, FinanceAgentA2A
from finance.workflows import FinanceAgentTaskWorkflow

FINANCE_AGENT_CARD = AgentCard(
    name="Finance Refund Agent", version="1.0.0",
    description="Owned by Finance. Assesses refund requests (fraud risk, approval limits) and issues refunds. "
                "Refunds above $100 require human approval (task state input-required).",
    skills=[AgentSkill(id="refund", name="Refund request", description="Approve and issue a customer refund")])

@service_handler(service=FinanceAgentA2A)
class FinanceAgentA2AHandler:
    @sync_operation
    async def get_agent_card(self, ctx: StartOperationContext, input: AgentCardRequest) -> AgentCard:
        return FINANCE_AGENT_CARD                       # must answer within 10 s (sync op)

    @nexus.workflow_run_operation
    async def send_message(self, ctx: nexus.WorkflowRunOperationContext, msg: A2AMessage) -> nexus.WorkflowHandle[A2ATask]:
        # One A2A task per Support conversation; the ID derives from the caller → idempotent on Nexus retries
        return await ctx.start_workflow(FinanceAgentTaskWorkflow.run, msg, id=f"a2a-task-{msg.context_id}")
```

*Finance agent Workflow: the A2A task lifecycle.*
```python
# finance/workflows.py
import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.strands_agents import TemporalAgent
from temporalio.strands_agents.workflow import activity_as_tool
with workflow.unsafe.imports_passed_through():
    from finance_a2a_api.a2a import A2AMessage, A2ATask, Artifact, Part, TaskStatus
    from finance.activities import fraud_score, refund_history, issue_refund, IssueRefundInput

AUTO_APPROVE_LIMIT_USD = 100.0          # enforced in code, not by the LLM

FINANCE_PROMPT = """You are the Finance Refund agent. Assess the refund request: call fraud_score and
refund_history, then answer with RISK=LOW|HIGH and a one-paragraph justification for the approver."""

@workflow.defn
class FinanceAgentTaskWorkflow:
    def __init__(self) -> None:
        self.task: A2ATask | None = None
        self.decision: tuple[str, str] | None = None        # ("approve" | "reject", approver)
        self.agent = TemporalAgent(
            system_prompt=FINANCE_PROMPT,
            tools=[activity_as_tool(fraud_score, start_to_close_timeout=timedelta(seconds=10)),
                   activity_as_tool(refund_history, start_to_close_timeout=timedelta(seconds=10))],
            model="bedrock", start_to_close_timeout=timedelta(seconds=60))

    def _set(self, state: str, text: str | None = None) -> None:
        msg = A2AMessage(role="agent", parts=[Part(kind="text", text=text)], message_id=str(workflow.uuid4()),
                         context_id=self.task.context_id, task_id=self.task.id) if text else None
        self.task.status = TaskStatus(state=state, message=msg)
        workflow.logger.info("A2A task %s -> %s", self.task.id, state)

    @workflow.run
    async def run(self, msg: A2AMessage) -> A2ATask:
        self.task = A2ATask(id=workflow.info().workflow_id, context_id=msg.context_id, status=TaskStatus("submitted"))
        req = next(p.data for p in msg.parts if p.kind == "data")
        amount = float(req["amount_usd"])

        self._set("working")
        assessment = str(await self.agent.invoke_async(f"Refund request: {req}. Support says: {msg.parts[0].text}"))

        if amount > AUTO_APPROVE_LIMIT_USD or "RISK=HIGH" in assessment:
            self._set("input-required", f"Refund of ${amount:.2f} needs Finance approval. Assessment: {assessment}")
            try:
                await workflow.wait_condition(lambda: self.decision is not None, timeout=timedelta(hours=24))
            except asyncio.TimeoutError:
                self._set("failed", "No approver decision within 24h.")
                return self.task
            if self.decision[0] == "reject":
                self._set("rejected", f"Refund rejected by {self.decision[1]}.")
                return self.task

        self._set("working")
        refund = await workflow.execute_activity(
            issue_refund, IssueRefundInput(order_id=req["order_id"], amount_usd=amount, idempotency_key=self.task.id),
            start_to_close_timeout=timedelta(seconds=30))
        approver = self.decision[1] if self.decision else "auto (≤ limit)"
        self.task.artifacts.append(Artifact(name="refund_confirmation", parts=[Part(kind="data", data={
            "refund_id": refund.refund_id, "amount_usd": amount, "approved_by": approver, "handled_by_pod": refund.pod})]))
        self._set("completed", f"Refund {refund.refund_id} issued for ${amount:.2f} (approved by {approver}).")
        return self.task

    @workflow.signal
    def approve(self, approver: str) -> None:
        self.decision = ("approve", approver)

    @workflow.signal
    def reject(self, approver: str) -> None:
        self.decision = ("reject", approver)

    @workflow.query
    def get_task(self) -> A2ATask:
        return self.task
```
- **Activities** (`finance/activities.py`):
  - `fraud_score` and `refund_history` read mock Finance data.
  - `issue_refund` calls a mock **payments service inside the cluster** (for example a tiny `payments-mock` Deployment and Service in `temporal-workers`). It returns `refund_id` plus `pod` (`os.environ["POD_NAME"]`), which shows the EKS hop. That backs the "Finance runs next to payments" story. Allow it in the NetworkPolicy (7b.6).
  - Pass the `idempotency_key` to the payments mock, so a retried Activity never refunds twice.
- *Resolved in v0.6 (ADR-0002, ADR-0009): built as sketched, with `approve`/`reject` taking an `ApproverDecision(approver, comment)` dataclass, sync signal handlers that ignore decisions outside `input-required`, the wait passed in the task's memo (`FINANCE_APPROVAL_TIMEOUT`, at most the 24 h published in the Agent Card), and `failed` on timeout. The caller budgets 25 h, so a missing approver reaches Support as an A2A `failed` task, not a Nexus timeout.*
- **Design choice: the LLM recommends, code enforces.** The Finance agent's model produces the risk assessment. The approval limit is a deterministic `if` in Workflow code. That is a good point to make in a refund demo.
- **Alternative for the approval step:** the Strands plugin's HITL pattern (`BeforeToolCallEvent.interrupt()` on an `issue_refund` tool, resumed by a signal; see `samples-python/strands_plugin/human_in_the_loop`). It keeps the pause inside the agent loop, but makes the approval rule less explicit.
- Finance calls Bedrock for its model turns, so the handler **needs** IRSA (7b.3) with `bedrock:InvokeModel` on its model. That role is separate from Support's AgentCore execution role, which is another visible team boundary.

*Optional variant: an operation backed by a standalone Activity* (⚠️ experimental):
- Subclass `temporalio.nexus.TemporalOperationHandler` and call `start_activity(...)` on the Nexus-aware client in `start_operation`.
- Derive the Activity ID from the Nexus request ID, and set a start-to-close or schedule-to-close timeout.
- It needs `activity.enableCallbacks: true` for `agent-services` (Phase 3), and a recent `temporalio`.
- Reference: `samples-python/nexus_standalone_activity`.
- Before you build on it, confirm that **1.32.1** supports it. The samples target a special CLI dev-server build.

**7b.5 Worker entrypoint.**
```python
# finance/worker.py
import asyncio, os
from strands.models.bedrock import BedrockModel
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.strands_agents import StrandsPlugin
from temporalio.worker import Worker
from finance.service_handler import FinanceAgentA2AHandler
from finance.workflows import FinanceAgentTaskWorkflow
from finance.activities import fraud_score, refund_history, issue_refund

async def main():
    # Pin the model: without a models mapping the plugin uses Strands' default BedrockModel() (a different model).
    plugin = StrandsPlugin(models={"bedrock": lambda: BedrockModel(
        model_id=os.environ["BEDROCK_MODEL_ID"], region_name=os.environ["AWS_REGION"])})
    client = await Client.connect(**ClientConfig.load_client_connect_config(), plugins=[plugin])
    await Worker(client, task_queue="agent-services-tq",
                 workflows=[FinanceAgentTaskWorkflow],
                 activities=[fraud_score, refund_history, issue_refund],
                 nexus_service_handlers=[FinanceAgentA2AHandler()]).run()

asyncio.run(main())
```
- Unversioned is fine for the first pass. To show Worker Versioning on both sides, add `deployment_config=WorkerDeploymentConfig(...)` and manage rollouts with `temporal worker deployment set-current-version`. The **Temporal Worker Controller** for Kubernetes is another option; it would contrast nicely with the AgentCore WCI. Do **not** add a compute config, because these Workers poll all the time.
- **Deployment manifest:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: agent-services-worker, namespace: temporal-workers }
spec:
  replicas: 2
  selector: { matchLabels: { app: agent-services-worker } }
  template:
    metadata: { labels: { app: agent-services-worker } }
    spec:
      serviceAccountName: agent-services-worker
      containers:
        - name: worker
          image: <acct>.dkr.ecr.us-east-1.amazonaws.com/agent-services-worker:<tag>
          env:
            - { name: TEMPORAL_ADDRESS,   value: "temporal-frontend.temporal.svc.cluster.local:7233" }
            - { name: TEMPORAL_NAMESPACE, value: "agent-services" }
            - { name: BEDROCK_MODEL_ID,   value: "global.anthropic.claude-opus-5-5" }
            - { name: AWS_REGION,         value: "us-east-1" }
            - { name: TEMPORAL_TLS,       value: "true" }
            - { name: TEMPORAL_TLS_SERVER_NAME,          value: "temporal-frontend.temporal.svc.cluster.local" }
            - { name: TEMPORAL_TLS_CLIENT_CERT_PATH,     value: /etc/temporal/tls/tls.crt }
            - { name: TEMPORAL_TLS_CLIENT_KEY_PATH,      value: /etc/temporal/tls/tls.key }
            - { name: TEMPORAL_TLS_SERVER_CA_CERT_PATH,  value: /etc/temporal/tls/ca.crt }
            - name: POD_NAME
              valueFrom: { fieldRef: { fieldPath: metadata.name } }
          resources: { requests: { cpu: 250m, memory: 256Mi }, limits: { memory: 512Mi } }
          volumeMounts:
            - { name: tls, mountPath: /etc/temporal/tls, readOnly: true }
      volumes:
        - name: tls
          secret: { secretName: eks-nexus-worker-tls }
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: agent-services-worker, namespace: temporal-workers }
spec:
  minAvailable: 1
  selector: { matchLabels: { app: agent-services-worker } }
```
- ⚠️ Verify the `TEMPORAL_TLS_*_PATH` env var names against the envconfig reference (`references/client-environment-configuration#tls-variables`). *Resolved in v0.1/v0.9: correct; used by the Finance pods on kind.*

**7b.6 NetworkPolicy.** This needs a CNI that enforces policies; on EKS, turn on the VPC CNI's network policy agent. It shows the in-cluster firewall model next to the security groups on the AgentCore side.
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: { name: agent-services-worker-egress, namespace: temporal-workers }
spec:
  podSelector: { matchLabels: { app: agent-services-worker } }
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: temporal } }
          podSelector: { matchLabels: { app.kubernetes.io/component: frontend } }   # ⚠️ Verify chart 1.7.0 labels  (resolved v0.9: correct)
      ports: [{ protocol: TCP, port: 7233 }]
    - to: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: kube-system } } }]
      ports: [{ protocol: UDP, port: 53 }, { protocol: TCP, port: 53 }]
    - ports: [{ protocol: TCP, port: 443 }]      # VPC endpoints (STS for IRSA, bedrock-runtime); tighten with ipBlock on the VPCE subnets
    - to: [{ podSelector: { matchLabels: { app: payments-mock } } }]   # Finance → in-cluster payments system
      ports: [{ protocol: TCP, port: 8080 }]
```
If you also add an ingress NetworkPolicy on the `temporal` namespace, allow 7233 from the `temporal-workers` namespace and from the NLB (target type `ip` uses VPC source IPs).

**7b.7 Deploy and verify the handler Worker.**
```bash
kubectl apply -f temporal-workers/            # namespace, Certificate, SA, Deployment, PDB, NetworkPolicy, payments-mock
kubectl -n temporal-workers rollout status deploy/agent-services-worker
kubectl -n temporal-workers get certificate eks-nexus-worker-tls      # READY=True
kubectl -n temporal-workers logs deploy/agent-services-worker | head
temporal task-queue describe --namespace agent-services --task-queue agent-services-tq   # pollers from the 2 pods
```

**7b.8 Create the Nexus Endpoint** (operator access as in 7.0):
```bash
temporal operator nexus endpoint create \
  --name finance-agent \
  --target-namespace agent-services \
  --target-task-queue agent-services-tq \
  --description-file finance-agent-card.md
temporal operator nexus endpoint get --name finance-agent
```
- **Publish the Agent Card as the endpoint description.** That way the A2A peer is discoverable in the Web UI's Nexus registry. Keep it in sync with `FINANCE_AGENT_CARD` (7b.4).
  ```markdown
  # Finance Refund Agent (A2A) · v1.0.0
  **Owner:** Finance · **Runs on:** EKS (`agent-services`)
  **Service:** `FinanceAgentA2A` — `get_agent_card` (sync), `send_message` (A2A task)
  **Skill `refund`:** assesses fraud risk and approval limits; issues refunds.
  Refunds > $100 return task state `input-required` until a Finance approver signals `approve`/`reject`.
  ```
- The endpoint name must equal `NEXUS_ENDPOINT` in the caller Workflow (5.6).
- Self-hosted endpoints have **no allowed-caller list**; that is a Cloud-only feature. Under option (c), any namespace in the cluster can call this endpoint.
- You can also create and inspect endpoints in the Web UI under **Nexus**.

**7b.9 Smoke-test the handler without AgentCore.** Do this before Phase 8, to isolate problems. Run a throwaway caller Worker locally, port-forwarded with the operator cert, on a **separate, unversioned** Task Queue in `agents`, for example `nexus-smoke-tq`. Start `SupportSupervisorWorkflow` on that queue. Do **not** poll `<TASK_QUEUE>`, because a non-serverless poller there would hide the WCI behaviour in Phase 8. Alternatively, call the endpoint as a Standalone Nexus Operation. That is pre-release, needs CLI ≥1.9.0, and ⚠️ needs server support you should verify. It also makes a nice "any A2A client can call Finance" moment. *Resolved in v0.4 for server 1.32.0 (CLI 1.9.1): works, but only after starting the dev server with `--dynamic-config-value nexusoperation.enableStandalone=true` (off by default, contrary to the docs). v0.4 also ships the smoke Workflow, `A2ASmokeWorkflow` on `nexus-smoke-tq`.*
```bash
temporal nexus operation execute --namespace agents --endpoint finance-agent \
  --service FinanceAgentA2A --operation get_agent_card --operation-id card-smoke --input '{}'
temporal nexus operation execute --namespace agents --endpoint finance-agent \
  --service FinanceAgentA2A --operation send_message --operation-id refund-smoke \
  --input '{"role":"user","message_id":"m1","context_id":"smoke-1","parts":[{"kind":"text","text":"Broken headphones"},{"kind":"data","data":{"order_id":"1042","amount_usd":40,"reason":"damaged"}}]}'
```

---

### Phase 8 — End-to-end test

1. Make sure **no** Worker is polling the Task Queue anywhere else.
2. Start a Workflow on `<TASK_QUEUE>` in `agents`, either with the CLI (`temporal workflow start --task-queue <TASK_QUEUE> --type <WorkflowType> …`) or from your client app.
3. Observe:
   ```bash
   agentcore logs runtime --name <RUNTIME_NAME> --qualifier temporal_v1 --tail
   agentcore logs runtime --since 30m --level error
   temporal worker deployment describe --namespace agents --name <DEPLOYMENT_NAME>
   temporal workflow list --namespace agents --query 'TemporalNamespaceDivision = "TemporalWorkerControllerInstance"'
   # If nothing is returned, try --namespace temporal-system (⚠️ Verify which namespace the WCI runs in)
   temporal workflow show --namespace agents --workflow-id '<WCI workflow ID from the list above>'
   ```
   ⚠️ Verify: the WCI workflow-ID format. The draft assumed `temporal-sys-worker-controller-instance:<DEPLOYMENT_NAME>:<BUILD_ID>`. Copy the ID from `workflow list` instead.
4. **Reading the WCI history** (⚠️ Verify: exact activity and event names):
   - **Success:** activities that complete for role validation (AssumeRole, then the check without the External ID), endpoint validation (`GetAgentRuntimeEndpoint`) and invocation (`InvokeAgentRuntime`).
   - **Failure:** `ActivityTaskFailed` events. Search the failure messages for `AccessDenied`, `ExternalId` / "does not require the configured external ID", `ValidationException`, `ResourceNotFoundException`, `ThrottlingException` and timeouts. Match them against §6.
5. Expected sequence:
   - The WCI history shows the invoke.
   - AgentCore logs show the Worker starting and connecting.
   - The Workflow completes.
   - After the debounce window, the Worker drains and the async task completes.
   - The session idles out after `idleRuntimeSessionTimeout`.
6. **Load test.**
   1. **Before you start:** create an AWS Budgets alert for the account or project, and a CloudWatch alarm on AgentCore Runtime session-count or vCPU metrics in the `bedrock-agentcore` namespace. ⚠️ Verify: the metric names in the console.
   2. Start **N = 50** Workflows. Expect peak concurrent sessions to be at most 50; under no-sync the WCI invokes as needed.
   3. **Success criteria:** all 50 complete, there are no WCI activity failures, sessions drain within debounce + `idleRuntimeSessionTimeout`, and active sessions return to 0.
   4. Scale up step by step. Watch the **25 new sessions per second** quota, the 1,000 TPS data-plane quota and the 150 TPS control-plane Get quota.
   5. `workercontroller.maxInstances` (default 100 per namespace) limits **Worker Deployment Versions with a compute config**, not sessions. A session load test will not hit it.
   6. **Clean up:**
      ```bash
      temporal workflow terminate --namespace agents \
        --query 'TaskQueue="<TASK_QUEUE>" AND ExecutionStatus="Running"' --reason loadtest-cleanup --yes
      ```
7. **Refund demo end to end: Support agent → Finance agent (A2A).** Prerequisites: Phase 7b is done, the smoke test in 7b.9 passed, and the throwaway caller is stopped. Mock data: order **1042** (headphones, $40, damaged) and order **2077** (laptop, $450, damaged).
   1. **Case A: a $40 refund, approved automatically, in one AgentCore session.**
      ```bash
      temporal workflow start --namespace agents --task-queue <TASK_QUEUE> --type SupportSupervisorWorkflow \
        --workflow-id support-1042 --input '"Order #1042: my headphones arrived broken. I want my money back."'
      ```
      - **Caller history** (`agents`) shows:
        - the A2A discovery (`FinanceAgentA2A.get_agent_card`)
        - the Supervisor's `invoke_model` turns and the Order Lookup and Return Policy sub-agent Activities
        - then **`NexusOperationScheduled` `FinanceAgentA2A.send_message` → endpoint `finance-agent`**, then Started and Completed.
      - **Finance side:** the Web UI link jumps to `a2a-task-support-1042` in **`agent-services`**. It shows the Finance agent's own model turns, `fraud_score`, `refund_history` and `issue_refund`.
      - **Result:** the Supervisor's customer reply quotes the refund ID. The `refund_confirmation` artifact shows `handled_by_pod`, an EKS pod.
   2. **Case B: a $450 refund that waits for a person, in two AgentCore sessions.**
      ```bash
      temporal workflow start --namespace agents --task-queue <TASK_QUEUE> --type SupportSupervisorWorkflow \
        --workflow-id support-2077 --input '"Order #2077: the laptop screen was cracked on arrival. Refund please."'
      # Watch the A2A task state on the Finance side:
      temporal workflow query --namespace agent-services --workflow-id a2a-task-support-2077 --type get_task
      #   → status.state = "input-required"
      ```
      - **Talking point:** "Finance is waiting for a human. Support's AgentCore session has **shut down**, so there's no compute and no cost, but the conversation is durable."
        - Show the AgentCore logs: the Worker drained.
        - Show `temporal workflow describe` on `support-2077`: one pending Nexus operation.
      - **Approve it as the Finance approver:**
        ```bash
        temporal workflow signal --namespace agent-services --workflow-id a2a-task-support-2077 \
          --name approve --input '"dana.finance"'
        ```
      - Expected:
        - The Finance task goes to `completed`.
        - The Nexus completion produces a new Workflow Task in `agents`.
        - The WCI history shows a **second** `InvokeAgentRuntime`.
        - AgentCore session #2 finishes the Supervisor's reply.
      - A variant: signal `reject` instead. Finance returns `rejected`, and the Supervisor writes an apology or escalation.
   3. **Observe both sides:**
      ```bash
      temporal workflow show --namespace agents --workflow-id support-2077                 # A2A hop as Nexus events
      temporal workflow show --namespace agent-services --workflow-id a2a-task-support-2077
      temporal workflow list --namespace agents --query 'TemporalNamespaceDivision = "TemporalWorkerControllerInstance"'
      agentcore logs runtime --name <RUNTIME_NAME> --qualifier temporal_v1 --since 15m
      kubectl -n temporal-workers logs deploy/agent-services-worker --since=15m | grep 'A2A task'   # submitted → working → input-required → completed
      ```
      In the Web UI, show three things side by side: the Supervisor's history, the Finance task history, and the **Nexus registry entry for `finance-agent`**, whose description is the Agent Card.
   4. **Connectivity talking points** (see the table in §1.3):
      - The AgentCore Worker's source IP is an ENI in the AgentCore subnets, going through the NLB. VPC Flow Logs on `sg-agentcore-worker` → NLB:7233 show this.
      - The EKS Worker connects pod-to-pod over the ClusterIP and never touches the NLB.
      - Neither Worker connects to the other.
   5. **Failure demo (optional).** Scale the EKS handler to 0: `kubectl -n temporal-workers scale deploy/agent-services-worker --replicas=0`.
      - The caller's Nexus operation stays pending, with retries visible in the UI's pending Nexus operation details.
      - The **WCI does not help here**, because `agent-services` has no compute config.
      - Scale back up and the operation completes. This shows that the two Worker types fail and recover differently.

### Phase 9 — Observability

- **Temporal:**
  - Server Prometheus metrics and Grafana dashboards. Watch `service_errors`, `persistence_latency`, `lock_latency`, `service_errors_resource_exhausted` and `tasks_dropped`.
  - `poller_scale_decision` is opt-in. It covers SDK poller autoscaling only and does **not** reflect WCI or AgentCore scaling.
  - Use WCI Workflow histories (`TemporalNamespaceDivision = "TemporalWorkerControllerInstance"`) to see invocation decisions.
  - **Nexus:**
    - Caller-side pending Nexus operations, with attempts and last failure, show in the Web UI and in `temporal workflow describe`.
    - Server metrics for Nexus requests and outbound callbacks appear on the frontend and history services. ⚠️ Verify the metric names in `common/metrics/metric_defs.go` (look for the `nexus_` prefix).
    - SDK metrics from the EKS handler Worker include `temporal_nexus_task_*`. Scrape them with a pod annotation or a ServiceMonitor after you turn on the SDK Prometheus exporter.
- **AgentCore:**
  - Logs go to `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`.
  - Metrics use the `bedrock-agentcore` namespace.
  - Traces need Transaction Search turned on once. `agentcore deploy` attempts this. To do it by hand, first add a CloudWatch Logs resource policy, then set the destination (or enable Transaction Search in the CloudWatch console):
    ```bash
    aws logs put-resource-policy --policy-name TransactionSearchXRayAccess --policy-document '{
      "Version":"2012-10-17","Statement":[{"Sid":"TransactionSearchXRayAccess","Effect":"Allow",
      "Principal":{"Service":"xray.amazonaws.com"},"Action":"logs:PutLogEvents",
      "Resource":["arn:aws:logs:us-east-1:<acct>:log-group:aws/spans:*",
                  "arn:aws:logs:us-east-1:<acct>:log-group:/aws/application-signals/data:*"],
      "Condition":{"ArnLike":{"aws:SourceArn":"arn:aws:xray:us-east-1:<acct>:*"},
                   "StringEquals":{"aws:SourceAccount":"<acct>"}}}]}'
    aws xray update-trace-segment-destination --destination CloudWatchLogs
    # optional: aws xray update-indexing-rule ...
    ```
    Copy the exact policy from observability-configure.html.
  - For custom spans, add `aws-opentelemetry-distro>=0.18.0` and run under `opentelemetry-instrument`. Set `DISABLE_ADOT_OBSERVABILITY=true` to use another OTel backend.
- **Cost:**
  - You pay per second of vCPU and GB actually used, up to the per-session maximum. CPU is not billed during I/O wait.
  - A session costs money for the whole debounce window plus active work.
  - Tune `AGENTCORE_DEBOUNCE_SECONDS` and the idle settings, and never leave an async task uncompleted.
  - Keep the Budgets alert and the session alarm from Phase 8 in place permanently.

### Phase 10 — Rolling out new Worker code

1. Bump `TEMPORAL_BUILD_ID` (and the code) in `agentcore.json` `envVars`. If you miss this, the new version's Build ID will not match.
2. Run `agentcore deploy --target default`. This creates a new **immutable Runtime version** `<N>`. Find `<N>` with `agentcore status --target default --json`.
3. Create a **new named endpoint** pointing at version `<N>`:
   ```bash
   agentcore add runtime-endpoint --runtime <RUNTIME_NAME> --name temporal_v2 --version <N>   # adds to "endpoints" in agentcore.json
   agentcore deploy --target default
   # control-plane equivalent:
   # aws bedrock-agentcore-control create-agent-runtime-endpoint --agent-runtime-id <id> --name temporal_v2 --agent-runtime-version <N>
   ```
   Get the endpoint ARN as in 5.5.
4. Update the invocation role's `AgentRuntimeARNs` only if the Runtime itself changed. The trailing `*` already covers new endpoints.
5. Run `create-version` with the **new Build ID** and the **new endpoint ARN**. Then run `set-current-version` immediately, or ramp with `temporal worker deployment set-ramping-version --percentage <p>`.
6. **Retire the old version:**
   ```bash
   temporal worker deployment describe-version --namespace agents --deployment-name <DEPLOYMENT_NAME> --build-id <OLD_BUILD_ID>
   # wait until drainage status shows drained (⚠️ Verify the exact field/value, e.g. DrainageStatus=drained)
   temporal worker deployment delete-version --namespace agents --deployment-name <DEPLOYMENT_NAME> --build-id <OLD_BUILD_ID>
   aws bedrock-agentcore-control delete-agent-runtime-endpoint --agent-runtime-id <id> --endpoint-name temporal_v1
   ```
   Also remove the old entry from `agentcore.json` `endpoints`. Clean up regularly because of the limit of 10 endpoints per agent. ⚠️ Verify: the delete-endpoint flag names with `--help`.
7. **Rollback:** run `set-current-version` back to the old Build ID while its endpoint still exists.
8. **Never** point a live version at `DEFAULT`. It floats to the latest version and causes non-determinism.
9. **Nexus contract changes.** The caller (AgentCore) and the handler (EKS) are deployed separately. Treat `finance_a2a_api` like a public API:
   - Add only optional fields.
   - Add new operations rather than changing existing ones.
   - Deploy the handler **before** the caller starts using a new operation.

   Handler Workflow code changes on EKS follow the usual Temporal versioning rules: patching, or Worker Versioning if you turned it on in 7b.5.

### Phase 11 — Backup, rollback and teardown

1. **Backups:** take an RDS snapshot before the first install, and before every Helm upgrade or schema change:
   ```bash
   aws rds create-db-snapshot --db-instance-identifier temporal-db --db-snapshot-identifier temporal-pre-<change>-$(date +%Y%m%d%H%M)
   ```
2. **Remove the Nexus side** (before you remove namespaces or the cluster):
   ```bash
   temporal operator nexus endpoint delete --name finance-agent
   kubectl delete namespace temporal-workers        # Deployment, Certificate, Secret, SA, NetworkPolicy
   ```
   Then delete the `agent-services-worker` IRSA role (if you created one) and the ECR repository. The cert-manager CA has no revocation (no CRL or OCSP). To cut off a leaked client cert, re-issue the root (2.6) or switch to authorization option (b).
3. **Turn off Serverless Workers:**
   - Set the current version back to a non-serverless version, or `temporal worker deployment delete-version` the serverless ones.
   - Set `workercontroller.enabled: false` in the values file and run `helm upgrade` (no pod restart).
4. **Remove AgentCore:** delete the named endpoints, then the runtime. Use `agentcore destroy` (⚠️ Verify: the 1.0.x command name with `agentcore --help`), or `aws bedrock-agentcore-control delete-agent-runtime-endpoint` and then `delete-agent-runtime`.
5. **Remove the invocation role:** `aws cloudformation delete-stack --stack-name temporal-agentcore-worker`.
6. Delete the IRSA role `temporal-server-wci` (and any OpenSearch/IAM-DB role), plus the Secrets Manager secrets `temporal/agentcore-worker-mtls` and `temporal/agentcore-external-id`. Secrets Manager applies a recovery window.
7. Run `helm uninstall temporal -n temporal`. RDS data **persists**; delete the instance separately, with a final snapshot, if you intend to.
8. AgentCore ENIs can linger for **up to 8 h**. Wait before you delete the subnets or security groups.
9. Terminate any leftover load-test and demo Workflows (Phase 8) in both `agents` and `agent-services`.

---

## 5. Validation checklist

**Foundation**
- [ ] Subnets are in AgentCore-supported AZ IDs, with DNS hostnames and DNS support enabled.
- [ ] The Route 53 private zone is associated with the VPC, and the frontend name resolves to the NLB from inside the VPC.
- [ ] VPC endpoints exist (`bedrock-agentcore`, `bedrock-agentcore-control`, `sts`, `logs`, `secretsmanager`, `bedrock-runtime`, plus the `s3` gateway with the `acr-code-*` policy; add `ecr.*` only for Container builds), or NAT is in place.
- [ ] Security groups:
  - AgentCore SG → NLB:7233 allowed, and → `sg-vpce`:443 allowed
  - NLB → pods:7233 allowed
  - pods → RDS:5432 allowed
  - intra-cluster ports open, if pod SGs or NetworkPolicies are used
- [ ] The deploying principal has `iam:CreateServiceLinkedRole` and `iam:PassRole`. Bedrock model access is enabled.
- [ ] An RDS snapshot exists from before the install.

**Temporal server**
- [ ] All pods run image `1.32.1`, with the planned replica counts and resources. `temporal operator cluster describe` shows the intended `historyShardCount`.
- [ ] Schemas are at core v1.19 and visibility v1.14. The RDS connection uses TLS with the mounted CA.
- [ ] Internode and frontend mTLS are on. The frontend `ca.crt` (or overridden `clientCaFiles`) trusts the Worker, UI and operator client CAs. The NLB is TCP passthrough. The `temporal operator namespace list` mTLS test passes.
- [ ] The Web UI has `web.tls.enabled: true` and connects to the frontend.
- [ ] The authorization decision (a/b/c) is recorded. If (c), the accepted risk is documented. The `agents` namespace exists.
- [ ] The dynamic config ConfigMap contains the three `workercontroller.*` keys, and they are present in the values file.
- [ ] The `temporal-worker` SA has the IRSA annotation. The worker pod has `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` set. A throwaway `amazon/aws-cli` pod with that SA returns the role from `sts get-caller-identity`.

**IAM**
- [ ] The WCI server role can only `sts:AssumeRole` the invocation role, and only the worker-service SA can use it. Any `es:ESHttp*` / `rds-db:connect` permissions sit on a separate role.
- [ ] The invocation role was created from the **self-hosted** template, trusts the server role's **role** ARN, and enforces the External ID. The External ID is stored in Secrets Manager.
- [ ] The invocation role policy resource is the Runtime ARN plus `*`.
- [ ] The execution role can read only the mTLS secret (and the IdP secret, under option (a)), and invoke only the chosen model.

**AgentCore and the Worker**
- [ ] The Runtime uses `authorizerType: AWS_IAM`. `get-agent-runtime` shows VPC mode with the right subnets and SGs. A named endpoint without hyphens is used, not DEFAULT.
- [ ] The env deployment name and Build ID match the CLI version. TLS env and the secret ID are wired.
- [ ] `complete_async_task` runs in `finally`, and an idle debounce is implemented.

**Nexus and the EKS handler**
- [ ] The `agent-services` namespace exists. The frontend cert has the `temporal-frontend.temporal.svc[.cluster.local]` SANs.
- [ ] The `eks-nexus-worker-tls` Certificate is Ready, and its CA is trusted by the frontend `ca.crt`.
- [ ] `agent-services-worker` has 2 ready pods. `temporal task-queue describe` shows its pollers on `agent-services-tq`.
- [ ] The NetworkPolicy is enforced. A pod without the label cannot reach the frontend on 7233.
- [ ] `temporal operator nexus endpoint get --name finance-agent` shows target `agent-services` / `agent-services-tq`.
- [ ] `workercontroller.enabled` is still constrained to `agents`. No Worker Deployment Version in `agent-services` has a compute config.
- [ ] The smoke test in 7b.9 passed without AgentCore, and nothing other than AgentCore polls `<TASK_QUEUE>`.

**End to end**
- [ ] `create-version` succeeds, `set-current-version` succeeds, and the WCI history shows a successful invoke.
- [ ] A Workflow completes with no other Workers running, and the session drains and ends.
- [ ] The Budgets alert and the session alarm exist. The load test meets its success criteria.
- [ ] **Refund demo Case A** (order 1042, $40): completes in one AgentCore session. The caller history shows `get_agent_card` and `send_message` Nexus events. `refund_confirmation.handled_by_pod` is an EKS pod.
- [ ] **Refund demo Case B** (order 2077, $450): `get_task` returns `input-required`, and the AgentCore session drains. After the `approve` signal, the WCI history shows a second `InvokeAgentRuntime`, and the Supervisor's reply quotes the refund ID.
- [ ] The Agent Card shows as the `finance-agent` endpoint description in the Web UI.
- [ ] A retried `issue_refund` with the same idempotency key does not create a second refund in `payments-mock`.

---

## 6. Troubleshooting and gotchas

| Symptom | Likely cause / fix |
|---|---|
| WCI never invokes anything | `compute_providers.enabled` lacks `aws-agentcore`. `scaling_algorithms` lacks `no-sync`. `workercontroller.enabled` is scoped to the wrong namespace. The version is not current. |
| AccessDenied on AssumeRole | The role is on the wrong pod or ServiceAccount; it must be on the worker service (`server.worker.serviceAccount`). `TemporalIamRoleArn` was given as an `assumed-role` session ARN. External ID mismatch. Pod Identity transitive session tags may need `sts:TagSession` (UNVERIFIED), so turn off session tags or use IRSA. An SCP or RCP blocks the call. |
| Error "role … does not require the configured external ID" | The trust policy lacks the `sts:ExternalId` condition (for example, it trusts the account root). Use the self-hosted template. |
| AccessDenied on GetAgentRuntimeEndpoint / InvokeAgentRuntime | The role policy resource is missing the trailing `*`, or the Runtime was recreated with a new ID. |
| ThrottlingException from AgentCore | The 1,000 TPS data-plane quota (shared, runtime-owner account) or the 150 TPS control-plane Get quota (not adjustable) was exceeded. |
| Timeouts calling AgentCore from the server | The `bedrock-agentcore-control` (control plane), `bedrock-agentcore` (data plane) or `sts` endpoint is missing, or private DNS is off. |
| Validation fails / wrong target | The Runtime ARN was passed instead of the **endpoint** ARN, or the DEFAULT endpoint was used. |
| Endpoint creation rejected | The endpoint name contains a hyphen. Use `temporal_v1`. |
| `agentcore validate` / `status --runtime` not found | AgentCore CLI 1.0.x syntax differs. Use `agentcore build` and `agentcore status --target default --json`. |
| `set-current-version` refuses with no pollers | The AgentCore Worker exited after the debounce window. Re-run straight after `create-version`, or add `--allow-no-pollers`. |
| Worker starts but cannot connect | SG egress to 7233 is missing. Subnet AZ IDs are not supported. TLS SAN mismatch (set `TEMPORAL_TLS_SERVER_NAME` to the Route 53 name). The Worker CA is not trusted by the frontend `ca.crt` / `clientCaFiles`. The NLB terminates TLS. The sample's `tls=bool(api_key)` was left in place. |
| Worker connects but gets PermissionDenied | `authorizer: default` is on and the Worker sends no JWT. See 2.7, option (a) or (c). |
| Worker connects but never receives tasks | The Build ID or deployment name in the env does not match the version. The version is not set current. Wrong namespace or Task Queue. |
| CodeZip runtime fails to start in VPC mode | The S3 gateway endpoint is missing, or its policy does not allow `acr-code-*-us-east-1-an` for `bedrock-agentcore.amazonaws.com`. Possibly no SG egress to the S3 prefix list. |
| Web UI cannot reach the frontend | `web.tls.enabled` is not set. Frontend mTLS does not configure the UI. |
| Sessions stay up for hours and the bill grows | `complete_async_task` is never called, or the debounce never fires. Temporal polling does **not** reset AgentCore's idle timer. |
| Activities fail around the 8 h mark | The session's `maxLifetime` was reached. Add heartbeats and timeouts so retries land on a new session. |
| Non-determinism after a deploy | A live version points at DEFAULT, or code changed under an existing endpoint. Use one immutable endpoint per Build ID. |
| Runtime creation fails in VPC mode | The subnet AZ ID is not supported, or the service-linked role cannot be created (`iam:CreateServiceLinkedRole` for network.bedrock-agentcore.amazonaws.com; see §3.2). |
| No traces in CloudWatch | The Logs resource policy for `xray.amazonaws.com` is missing. Run `put-resource-policy` before `update-trace-segment-destination`. |
| Cannot delete a subnet or SG | AgentCore ENIs can linger up to 8 h after an agent is deleted. |
| `agentcore` command behaves unexpectedly | The old pip starter toolkit shadows the npm CLI. Uninstall it. |
| `jq`/`psql`/`aws` not found in a pod | The admin-tools and server images do not include them. Use your workstation, a `postgres:16` pod or an `amazon/aws-cli` pod. |
| List queries error after upgrading to 1.32 | The unified query converter rejects type mismatches. Fix the queries, or temporarily set `system.visibilityEnableUnifiedQueryConverter: false`. |
| Frontend-only mTLS breaks the cluster | Internode TLS is also needed, and internode certs need no EKUs or both EKUs. |
| Database EOF errors | Set `maxConnLifetime`. For RDS PG 15+, enable TLS with the mounted RDS CA (⚠️ Verify). |
| Caller Nexus operation stuck in "Scheduled" with retries | No handler Worker is polling `agent-services-tq`, the endpoint's target namespace or Task Queue is misspelled, or `NEXUS_ENDPOINT` doesn't match the endpoint name. Check `temporal operator nexus endpoint get` and the handler pod logs. The UI shows the last attempt failure on the pending operation. |
| "endpoint not found" in the caller | The endpoint was not created, or it was created on a different cluster. Nexus endpoints are per Temporal Service. |
| EKS Worker TLS error "certificate is valid for temporal-frontend.temporal-customer-support.internal, not temporal-frontend.temporal.svc…" | Add the in-cluster SANs to the frontend cert (2.5), or set `TEMPORAL_TLS_SERVER_NAME` to the Route 53 name. |
| EKS Worker "connection refused" or timeouts, but other clients work | The NetworkPolicy blocks 7233 or DNS (53). The frontend pod label selector in 7b.6 doesn't match chart 1.7.0's labels. |
| Nexus operation fails after about 10 s | It is a `@sync_operation` doing too much work. Use `@workflow_run_operation`. |
| Caller completes the Nexus call, but the second Workflow Task waits a long time | Session #1 drained, and the WCI had to start session #2 (expected). Raise `AGENTCORE_DEBOUNCE_SECONDS` to keep the session warm across short Nexus calls. That trades cost for latency. |
| Standalone-Activity handler fails with "completion callbacks are not enabled for this namespace" | `activity.enableCallbacks` is not set for `agent-services` (Phase 3), or the server build doesn't support it. |
| The WCI tries to invoke something for `agent-services` | `workercontroller.enabled` has no namespace constraint, or a version in `agent-services` was given a compute config. |
| `ask_finance_agent` fails with a "not in workflow" error, or a sandbox or determinism error | The tool was wrapped with `activity_as_tool`. It must be a plain in-workflow `@tool`, because the Nexus client works only in Workflow context. Or use the explicit-handoff fallback (5.6). |
| The Supervisor answers without calling Finance (it "approves" the refund itself) | Prompt drift. Tighten `SUPERVISOR_PROMPT` ("You CANNOT issue refunds"), or force the tool call. For a guaranteed hop, use the explicit-handoff fallback. |
| `send_message` starts a second Finance task for the same ticket | The Workflow ID is not derived from `context_id`. Keep `id=f"a2a-task-{msg.context_id}"`, so Nexus retries are idempotent. |
| `get_task` query returns null | It was queried before the first Workflow Task ran, or the Workflow ID is wrong. Use `a2a-task-<support workflow id>`. |
| The approval signal has no effect | It was sent to the `agents` namespace or to the Support Workflow. Approval goes to `a2a-task-…` in **`agent-services`**. |
| Finance model calls fail with AccessDenied | The handler's IRSA role (7b.3) lacks `bedrock:InvokeModel` for its model, or the pod's ServiceAccount is not annotated. |

---

## 7. Open questions to confirm with Temporal and AWS

**Temporal**
1. ~~Which service runs the WCI?~~ **Resolved:** the worker service (`service/worker/fx.go`@v1.32.0 registers `wcicomponent.Module`). The docs still say only "the Temporal Service", so it is worth asking Temporal to document this.
2. ~~Which Helm keys scope a ServiceAccount to the worker service?~~ **Resolved:** `server.worker.serviceAccount.{create,name,extraAnnotations}` in chart 1.7.0.
3. ~~Is IRSA or Pod Identity recommended?~~ **Decided: IRSA** (4.1). Still worth asking Temporal: does Pod Identity (transitive session tags) require `sts:TagSession` on the invocation-role trust?
4. What is the minimum `temporalio` Python version for a plain AgentCore Worker? (The sample pins ≥1.34.0.)
5. Is there a recommended pattern for **self-hosted mTLS** from AgentCore (envconfig plus Secrets Manager)? This design uses mTLS (option (c)), so the JWT question is deferred.
6. Is there a self-hosted equivalent of Cloud's "Validate Connection"? Which namespace does the WCI Workflow run in, what is its ID format, and what activity names should you search in its history?
7. Are `…aws.intermediary_roles` (and their JSON field names), `…periodic_validation_interval_s` and `…hook.min_signal_interval_*` supported in 1.32.x? (`workercontroller.maxInstances` is confirmed: default 100, limits the number of versions with a compute config per namespace.)
8. Will there be a chart release for 1.32.1, or is overriding the image tag on chart 1.7.0 the intended path?
9. Does the OSS UI 2.54.x show compute-provider config and status for Worker Deployment Versions?
10. Does the server hot-reload rotated TLS certificates?
11. Is Aurora (PostgreSQL or MySQL) supported? What `version` value should the datastore use for OpenSearch 2.x?
12. Which principal attribution key applies to 1.32: `frontend.enablePrincipalPropagation` or `system.enablePrincipalAttribution`?
13. Does `set-current-version` for AgentCore versions need `--allow-no-pollers`?
14. Does self-hosted multi-cluster replication (XDC) work with Serverless Workers?
15. **Nexus + Serverless Workers:** is there any known issue with a Serverless (WCI-managed) Worker as a **Nexus caller**? In particular, does a Workflow Task produced by a Nexus completion callback reliably trigger the WCI when nothing is polling?
16. Does stock server **1.32.1** support Nexus operations backed by a standalone Activity (`activity.enableCallbacks`, `TemporalOperationHandler.start_activity`), or does that still need a pre-release build? And Standalone Nexus Operations from the CLI? *CLI half resolved in v0.4 for 1.32.0: yes, with `nexusoperation.enableStandalone=true`; the Activity-backed variant is descoped (ADR-0004).*
17. With option (b) (custom ClaimMapper plus the default Authorizer), which identity and namespace are checked when a caller Workflow dispatches to a Nexus endpoint that targets another namespace?
18. Does the Python SDK reload file-based client certs after cert-manager rotates them, or must the EKS Worker restart?
19. **Strands plugin and the A2A tool:** does `temporalio-strands-agents` run an `async` plain `@tool` on the Workflow event loop, so that it can `await` a Nexus operation for hours? Is there a planned `nexus_operation_as_tool` for Strands, like the one in the OpenAI Agents integration? *First half resolved in v0.5: yes. Second half asked (`docs/ask-temporal.md`).*
20. **Strands plugin, agents as tools:** is calling a nested `TemporalAgent.invoke_async` from an in-workflow `@tool` supported? *Resolved in v0.3: yes (ADR-0003, `spikes/nested_agents.py`).*
21. Can the caller attach a summary or label to a Nexus operation in Python, so that the Web UI shows "A2A → Finance Refund Agent" on the event? *Resolved in v0.4: yes, `execute_operation(..., summary=...)`.*

**AWS**
1. ~~What is the `agentcore.json` schema for VPC subnets and SGs?~~ **Resolved:** `networkMode: "VPC"` plus `networkConfig: {subnets, securityGroups, vpcId?}` (agentcore-cli `src/projectSchemas/runtime.ts`).
2. Which VPC endpoint does Bedrock model access need from a VPC-mode Runtime (expected: `bedrock-runtime`)? Is an STS endpoint needed? Is SG egress to the S3 prefix list needed for CodeZip?
3. ~~What is the procedure for creating extra named endpoints?~~ **Resolved:** `agentcore add runtime-endpoint --runtime <r> --name <n> --version <N>`, then `agentcore deploy`, or `CreateAgentRuntimeEndpoint`.
4. Can a Runtime inject Secrets Manager values natively, or must the entrypoint fetch them?
5. What IAM actions does Code Interpreter need (if your Activities use it)?
6. What are the Terraform resource names for AgentCore Runtime and endpoints? Is the CloudFormation resource `AWS::BedrockAgentCore::RuntimeEndpoint`?
7. Are there regional pricing differences? Which microVM pricing generation (v1 or v2) applies to new Runtimes?
8. What are the exact CloudWatch metric names for active sessions and vCPU, for alarms?

---

## 8. References

**Temporal: Serverless Workers / AgentCore**
- https://docs.temporal.io/serverless-workers
- https://docs.temporal.io/serverless-workers/agentcore
- https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers
- https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/agentcore
- https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/agentcore/self-hosted-setup
- https://docs.temporal.io/assets/files/temporal-self-hosted-serverless-worker-agentcore-role-d633a078183a821913145e7fb41bb8cb.yaml (the hash can change; use the download link on the self-hosted setup page)
- https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/aws-lambda/self-hosted-setup
- https://docs.temporal.io/develop/python/workers/serverless-workers/agentcore
- https://docs.temporal.io/guides/durable-agent-on-agentcore
- https://docs.temporal.io/references/client-environment-configuration#tls-variables
- https://temporal.io/blog/amazon-bedrock-agentcore-with-temporal-serverless-workers
- https://github.com/temporalio/samples-python/tree/main/bedrock_agentcore/strands_agent
- https://github.com/temporalio/documentation-sdk-code-examples/blob/main/python-sandbox/workers/agentcore_worker/worker.py
- https://github.com/temporalio/temporal-auto-scaled-workers (wci/client/config.go at v0.2.0-1.32.0.158.0, wci/workflow/compute_provider/aws.go)
- https://github.com/temporalio/cli/releases/tag/v1.8.3
- https://github.com/temporalio/cli/blob/main/internal/temporalcli/commands.yaml
- https://github.com/temporalio/samples-dotnet/blob/main/src/LambdaWorker/README.md and https://github.com/temporalio/samples-java/blob/main/lambda-worker/README.md (`--allow-no-pollers`)

**Temporal: Nexus**
- https://docs.temporal.io/production-deployment/self-hosted-guide/nexus
- https://docs.temporal.io/references/dynamic-configuration#nexus-settings
- https://github.com/temporalio/temporal/blob/main/docs/architecture/nexus.md
- https://docs.temporal.io/develop/python/nexus/feature-guide
- https://github.com/temporalio/sdk-python/blob/main/README.md (Nexus section: `workflow_run_operation`, 10 s sync limit)
- https://docs.temporal.io/cli/command-reference/operator#nexus
- https://docs.temporal.io/nexus/standalone-activity
- https://github.com/temporalio/samples-python/tree/main/nexus_standalone_activity
- https://docs.temporal.io/develop/python/nexus/standalone-operations

**Agents: Strands plugin and A2A**
- https://docs.temporal.io/develop/python/integrations/strands-agents (in-workflow `@tool` vs `activity_as_tool`, `StrandsPlugin`, `TemporalAgent`)
- https://github.com/temporalio/samples-python/tree/main/strands_plugin (`tools`, `human_in_the_loop`, `structured_output`)
- https://docs.temporal.io/develop/python/integrations/openai-agents#nexus-operation-tools (`nexus_operation_as_tool`, the same pattern in another integration)
- https://docs.temporal.io/guides/durable-agent-on-agentcore
- https://a2a-protocol.org (A2A spec: Agent Card, Message/Part, Task/TaskState, Artifact). ⚠️ Verify field names against the spec version you present.

**Temporal: server, self-hosting, Helm**
- https://github.com/temporalio/temporal/releases/tag/v1.32.0
- https://github.com/temporalio/temporal/releases/tag/v1.32.1
- https://github.com/temporalio/temporal/releases/tag/v1.31.0
- https://github.com/temporalio/temporal/releases/tag/v1.30.1
- https://github.com/temporalio/temporal/blob/v1.32.0/service/worker/fx.go
- https://github.com/temporalio/temporal/blob/v1.32.1/docker/targets/server.Dockerfile
- https://github.com/temporalio/temporal/blob/v1.32.1/docker/targets/admin-tools.Dockerfile
- https://github.com/temporalio/temporal/tree/v1.32.1/tools/elasticsearch
- https://github.com/temporalio/temporal/blob/v1.32.1/common/metrics/metric_defs.go
- https://github.com/temporalio/helm-charts/blob/main/README.md
- https://github.com/temporalio/helm-charts/tree/temporal-1.7.0/charts/temporal (values.yaml, values/values.postgresql.yaml, templates/_helpers.tpl, server-deployment.yaml, server-dynamicconfigmap.yaml, web-deployment.yaml, serviceaccount.yaml, server-job.yaml)
- https://raw.githubusercontent.com/temporalio/helm-charts/temporal-1.7.0/charts/temporal/Chart.yaml
- https://raw.githubusercontent.com/temporalio/helm-charts/main/UPGRADING.md
- https://docs.temporal.io/self-hosted-guide/deployment
- https://docs.temporal.io/self-hosted-guide/production-checklist
- https://docs.temporal.io/self-hosted-guide/upgrade-server
- https://docs.temporal.io/self-hosted-guide/visibility
- https://docs.temporal.io/self-hosted-guide/visibility/postgresql
- https://docs.temporal.io/self-hosted-guide/visibility/elasticsearch
- https://docs.temporal.io/temporal-service/persistence
- https://docs.temporal.io/self-hosted-guide/security
- https://docs.temporal.io/references/service-configuration
- https://docs.temporal.io/references/dynamic-configuration
- https://docs.temporal.io/self-hosted-guide/monitoring
- https://docs.temporal.io/references/web-ui-environment-variables
- https://github.com/temporalio/dashboards
- https://temporal.io/blog/scaling-temporal-the-basics

**AWS**
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-cli.html
- https://github.com/aws/agentcore-cli/blob/main/command.md
- https://github.com/aws/agentcore-cli/blob/main/src/projectSchemas/runtime.ts
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html
- https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html
- https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntimeEndpoint.html
- https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-runtime.html
- https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrockagentcore/RuntimeProps.html
- https://aws.amazon.com/bedrock/agentcore/pricing/
- https://docs.aws.amazon.com/eks/latest/userguide/associate-service-account-role.html
- https://docs.aws.amazon.com/eks/latest/userguide/pod-id-role.html
- https://docs.aws.amazon.com/eks/latest/userguide/pod-id-abac.html
- https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/service/annotations/
- https://cert-manager.io/docs/configuration/ca/ (CA issuer) and https://cert-manager.io/docs/configuration/selfsigned/ (bootstrapping a root)
- https://registry.terraform.io/modules/terraform-aws-modules/eks/aws and https://registry.terraform.io/modules/terraform-aws-modules/vpc/aws
- https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html (global inference profiles and IAM)
- https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-5-5.html (Opus 5.5 model card: profile IDs, geo routing, supported APIs and tool_choice)
- https://docs.aws.amazon.com/secretsmanager/latest/userguide/ascp-pod-identity-integration.html
- https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html

**Community (lower confidence)**
- https://community.temporal.io/t/3668 and https://community.temporal.io/t/4994 (ALB vs NLB)
- https://community.temporal.io/t/1004 (Aurora MySQL tx_isolation)
- https://community.temporal.io/t/15533 (shard sizing)
- https://community.temporal.io/t/183 (frontend TLS requires internode TLS)
