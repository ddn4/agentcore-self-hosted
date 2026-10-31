# Versions actually tested

Pre-release software (Serverless Workers, the Strands plugin) is pinned to what
was tested. `make doctor` reads the **Temporal CLI** row; `tests/test_harness.py`
asserts that it matches `demo_testing.TEMPORAL_CLI_VERSION` and the installed CLI.
Python packages are locked in `uv.lock`; the table records the resolved versions.

## Tools

| Tool | Version | Since | Notes |
|---|---|---|---|
| Temporal CLI | 1.9.1 | v0.1 | embeds server 1.32.0 and UI 2.54.1; `make dev-server`, `start_local(dev_server_existing_path=...)` |
| Temporal dev server | 1.32.0 | v0.1 | from the CLI; the Helm/EKS server is 1.32.1 (v0.9+) |
| Temporal server image | 1.32.1 | v0.9 | `temporalio/server:1.32.1`, `temporalio/admin-tools:1.32.1` (arm64 manifests); the chart's appVersion is 1.32.0, so pods still carry the label `app.kubernetes.io/version=1.32.0` |
| Temporal Helm chart | 1.7.0 | v0.9 | `--repo https://go.temporal.io/helm-charts`; the package itself contains `values/*.yaml` and `ci/*.yaml` |
| Temporal UI image | 2.54.1 | v0.9 | chart default |
| cert-manager | v1.21.3 | v0.9 | jetstack chart, `crds.enabled=true` |
| kind node image | kindest/node:v1.37.0 | v0.9 | kind 0.33.0 default; arm64, containerd 2.3.4, kindnet (enforces NetworkPolicy) |
| PostgreSQL (kind) | postgres:16 | v0.9 | in-cluster stand-in for RDS |
| Python base image | python:3.12.12-slim-bookworm | v0.9 | Finance and payments-mock images; uv from `ghcr.io/astral-sh/uv:0.12.15` |
| kubeconform | v0.7.0 | v0.9 | project-local (`make tools`, pinned sha256) |
| uv | 0.12.15 | v0.1 | |
| Python | 3.12.14 | v0.1 | uv-managed; matches AgentCore `PYTHON_3_12` |
| Node.js | 24.16.0 | v0.1 | >= 20.12 required for `@aws/agentcore` (v0.8) |
| AWS CLI | 2.36.46 | v0.1 | |
| kind | 0.33.0 | v0.1 (doctor) | used from v0.9 |
| Helm | 4.3.1 | v0.1 (doctor) | used from v0.9 |
| kubectl | 1.37.0 | v0.1 (doctor) | used from v0.9 |
| colima | 0.10.3 | v0.1 (doctor) | docker context `colima-agentcore` (6 CPU / 12 GB) |
| gitleaks | not installed | — | `scrub-check` falls back to grep locally; CI runs gitleaks |
| Terraform | 1.17.0 | v0.8 | project-local (`make tools`, sha256 from `terraform_1.17.0_SHA256SUMS`); stacks require >= 1.10 |
| Terraform AWS provider | 6.71.0 | v0.8 | `~> 6.70`; `.terraform.lock.hcl` committed per stack (darwin_arm64, linux_amd64) |
| tflint | v0.64.0 | v0.8 | project-local (`make tools`, sha256 from the release `checksums.txt`); bundled `terraform` ruleset 0.15.0, preset `recommended` (no AWS plugin: it needs a GitHub download per run) |
| `@aws/agentcore` CLI | 1.0.1 | v0.8 | `make tools-agentcore` → `.tools/agentcore-cli` (npm, exact version; Node >= 20.12); via `scripts/agentcore.sh` (telemetry off, Transaction Search off) |
| `@aws/agentcore-cdk` / aws-cdk / aws-cdk-lib | 1.0.0 / 2.1126.0 / 2.269.0 | v0.8 | installed by the CLI's project template in `build/agentcore-project/agentcore/cdk` |
| Self-hosted invocation-role CFN template | sha256 `033ff21ba09a56d31d611f661a65782837b0cfb30f74698bb84efd6999a1d4e3` | v0.8 | `deploy/agentcore/temporal-self-hosted-serverless-worker-agentcore-role.yaml` (`make cfn-template`; URL in `scripts/cfn-template.sh`) |
| Terraform module `terraform-aws-modules/vpc/aws` | 6.7.3 (`~> 6.7`) | v0.10 | runbook sketch said `~> 5.0`; v6 takes AZ IDs in `azs` (sets `availability_zone_id`) |
| Terraform module `terraform-aws-modules/eks/aws` | 21.29.0 (`~> 21.29`) | v0.10 | runbook sketch said `~> 20.0`; v21 inputs `name`, `kubernetes_version`, `endpoint_*`, `addons`, `encryption_config = null` |
| Terraform module `terraform-aws-modules/iam/aws` (`iam-role-for-service-accounts`) | 6.8.2 (`~> 6.8`) | v0.10 | the LBC's IRSA role and policy only; the two app roles use `infra/terraform/modules/irsa-role` |
| Terraform Helm provider | 3.3.0 (`~> 3.3`) | v0.10 | `kubernetes = { host, cluster_ca_certificate, exec = {...} }` (v3 attribute syntax); exec `aws eks get-token` |
| EKS Kubernetes | 1.37 | v0.10 | standard-support default on 2026-10-31 (1.34–1.37 standard); same minor as the kind node (v0.9). Add-ons (most recent at create): vpc-cni v1.23.x with `enableNetworkPolicy`, coredns v1.14.x, kube-proxy v1.37.x |
| EKS node AMI | `AL2023_ARM_64_STANDARD` on m7g.large | v0.10 | ADR-0006; fallback m6i.xlarge + `AL2023_x86_64_STANDARD` |
| AWS Load Balancer Controller chart | 3.6.2 (app v3.6.2) | v0.10 | `https://aws.github.io/eks-charts`, `helm_release` in `20-platform` |
| cert-manager chart (EKS) | v1.21.3 | v0.10 | same as kind; `helm_release`, `crds.enabled=true` |
| RDS PostgreSQL | **16.15** | v0.10 | Temporal tests 16.6, which RDS no longer offers (2026-10-31: 16.9–16.15 available); latest 16 minor chosen, `auto_minor_version_upgrade = false` (ADR-0006). Parameter group `temporal-db-pg16` (family `postgres16`, `rds.force_ssl = 1`, also the family default); CA `rds-ca-rsa2048-g1` |
| Test images (verify v0.10) | `public.ecr.aws/docker/library/busybox:1.37`, `…/postgres:16`, `public.ecr.aws/aws-cli/aws-cli:2.36.46` | v0.10 | public ECR (no Docker Hub rate limit) |
| Temporal on EKS (v0.11) | chart 1.7.0, server/admin-tools 1.32.1, UI 2.54.1 | v0.11 | same values as kind (`values.postgresql/base/tls`) + `values.eks.yaml` + generated IDs; frontend ×2, others ×1; 512 shards; schemas core **1.19**, visibility **1.14** on RDS PostgreSQL 16.15 |
| RDS CA bundle | `https://truststore.pki.rds.amazonaws.com/us-east-1/us-east-1-bundle.pem` (3 CAs, read 2026-10-31) | v0.11 | Secret `temporal/rds-ca`; the instance uses `rds-ca-rsa2048-g1` |
| Internal NLB | AWS Load Balancer Controller 3.6.2 (v0.10), Service annotations | v0.11 | `temporal-frontend`: network / internal / ip targets / TCP 7233 + 7243, SG `agentcore-demo-nlb-temporal-frontend` |
| Finance / payments-mock images on EKS | `linux/arm64`, ECR tags `img-<12 hex of the local image ID>` | v0.11 | built in colima (`DOCKER_CONTEXT=colima-agentcore`), pushed by `make finance-image` |
| Test images (verify v0.11) | `public.ecr.aws/docker/library/alpine:3.22` (+ `apk add openssl bind-tools`), `…/postgres:16`, `…/busybox:1.37`, `public.ecr.aws/aws-cli/aws-cli:2.36.46`, `temporalio/admin-tools:1.32.1` | v0.11 | throwaway pods, deleted after each check |

## Python packages (uv.lock at v0.1)

| Package | Version | Constraint | Notes |
|---|---|---|---|
| temporalio | 1.35.0 | `>=1.34.0,<2` | 1.34.0 is the plugin's minimum |
| temporalio-strands-agents | 0.1.0 | `>=0.1.0` | import path `temporalio.strands_agents` |
| strands-agents | 1.62.0 | `>=1.39.0` | |
| nexus-rpc | 1.4.0 | `>=1.1.0,<2` | the contract (`finance_a2a_api`, v0.4) and Finance |
| bedrock-agentcore | 1.25.1 | `>=1.9.1` | Support only (AgentCore entrypoint, v0.7) |
| boto3 / botocore | 1.43.122 | `>=1.34.92,<2` | |
| httpx | 0.28.1 | `>=0.27` | Finance → payments-mock (v0.4) |
| pydantic | 2.14.2 | (transitive) | the plugin installs the pydantic data converter |
| ruff | 0.17.3 | dev | |
| pyright | 1.1.414 | dev | |
| pytest / pytest-asyncio | 9.2.0 / 1.4.1 | dev | |
| honcho | 2.0.0 | dev | `make dev` |

## Model

| Item | Value |
|---|---|
| Model | Claude Opus 5.5 on Bedrock |
| Default profile | `global.anthropic.claude-opus-5-5` (`BEDROCK_MODEL_ID`) |
| US-only profile | `us.anthropic.claude-opus-5-5` (same code; IAM differs) |
| Region | `us-east-1` (`AWS_REGION`) |
| Effort | `BEDROCK_EFFORT` (`low`…`max`), default `medium` = model default, not sent (ADR-0001) |

## Effort latency (v0.2)

Per Supervisor turn (Case A first turn, tools attached), through the plugin's
`invoke_model` Activity: `spikes/opus_params.py --turns 3`.

| Effort | Latency per turn (s) | Output tokens | Measured |
|---|---|---|---|
| low | 3.20, 3.93, 2.94 (median 3.20) | 78, 78, 78 | 2026-10-30, us-east-1, `global.` profile (v0.9.2) |
| medium | 3.81, 4.46, 3.69 (median 3.81) | 158, 136, 152 | 2026-10-30, us-east-1, `global.` profile (v0.9.2) |

## Protocols (v0.4)

| Protocol | Version | Notes |
|---|---|---|
| A2A (shape only) | 0.3.0 | `finance_a2a_api` follows A2A 0.3.0 concepts in snake_case; A2A 1.0.0 deltas in `packages/finance-a2a-api/README.md` |
| Nexus on the dev server | CLI 1.9.1 / server 1.32.0 | Standalone Nexus Operations need `--dynamic-config-value nexusoperation.enableStandalone=true` (set by `scripts/dev-server.sh`) |
| WCI on the dev server (v0.7) | CLI 1.9.1 / server 1.32.0 | Present. `make dev-server WCI=1`: `workercontroller.enabled=true`, `compute_providers.enabled=["aws-agentcore"]`, `scaling_algorithms.enabled=["no-sync"]`, `compute_providers.aws.require_role_and_external_id=false`, `AWS_ENDPOINT_URL` → `scripts/agentcore_emulator.py` |
| AgentCore runtime HTTP contract (v0.7) | `bedrock-agentcore` 1.25.1 | `/invocations`, `/ping` (`Healthy`/`HealthyBusy` from async tasks); CodeZip bundle 43 MB zipped for linux arm64 / CPython 3.12 |
| AgentCore CLI project (v0.8) | `agentcore.json` `"version": 2` | CodeZip deps from `pyproject.toml` (`uv pip install -r pyproject.toml --python-platform aarch64-manylinux2014 --only-binary :all:`); asset 42 MB; runtime `AgentcoreDemo_default_support_supervisor` (`<project>_<target>_<runtime>`, seen live), stack `AgentCore-AgentcoreDemo-default` |
| WCI on AWS (v0.8) | dev server 1.32.0 (CLI 1.9.1), WCI AWS SDK `aws-sdk-go-v2/1.43.4` | `WCI=aws` with `AWS_PROFILE=temporal-dev-wci` (SSO `source_profile` → AssumeRole; followed by the Go SDK). Activities `ValidateSpec`, `InvokeWorkersToRegisterTaskQueues`, `InvokeWorker`, `PullStats` (~60 s); fresh AssumeRole with External ID per invoke; AgentCore session start ~6–8 s (PUBLIC, CodeZip, 42 MB) |
