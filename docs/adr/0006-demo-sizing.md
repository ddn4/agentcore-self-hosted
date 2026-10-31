# ADR-0006: Demo sizing deviations from the runbook

- Status: **Accepted by the user** (plan §3.6, confirmed at v0.9); written and applied at
  v0.10-aws-foundation (2026-10-31).
- Runbook: 1.1, 1.2, 1.3, 1.4, 2.x (`numHistoryShards`), 5.3 (debounce); plan §3.3, v0.10, §8.

## Context

The runbook sizes a small production install: `m6i.xlarge` × 3 (min 3, max 9), RDS
`db.r6g.large` Multi-AZ with 100 GB and deletion protection, interface endpoints in every
subnet, and a 60 s AgentCore idle debounce. This repo is a demo that runs for a few days at a
time, on one account with a USD 200/month budget (`00-guardrails`), and is torn down between
presentations. Every always-on dollar counts; correctness and the story must not change.

## Decision

| Item | Runbook | Demo | Why it is safe here |
|---|---|---|---|
| EKS nodes | 3 × m6i.xlarge (x86) | **2 × m7g.large** (arm64, min 2, max 3), AMI `AL2023_ARM_64_STANDARD` | v0.9 proved every image arm64 (server 1.32.1, admin-tools, UI, postgres, cert-manager, our Finance/payments-mock). Fallback kept: `node_instance_type = "m6i.xlarge"`, `node_ami_type = "AL2023_x86_64_STANDARD"` and amd64 builds |
| RDS | db.r6g.large, Multi-AZ, 100 GB, deletion protection, backups 7 d | **db.t4g.medium, single-AZ, 20 GB gp3**, encrypted, `deletion_protection = false`, backups 1 d, final snapshot on destroy | demo load is a handful of Workflows; a lost AZ costs a re-run, not data anyone needs |
| PostgreSQL minor | 16.6 (Temporal-tested) | **16.15** | RDS no longer offers 16.6 (available on 2026-10-31: 16.9–16.15). Same major as Temporal's tested 16.6; the latest minor has the longest RDS support window; `auto_minor_version_upgrade = false` keeps it pinned (`docs/versions.md`) |
| Interface endpoints | in every private subnet | **2 of the 3 private subnets** (`endpoint_subnet_count = 2`) | private DNS answers with the endpoint ENIs from any AZ; saves 6 × $0.01/h. Set 3 for parity |
| NAT | one per AZ | **one** (`single_nat_gateway`) | one EIP (the account is at 8/10); AZ failure of the NAT is acceptable for a demo |
| Customer KMS key for EKS secrets | (module default) | **none** (`encryption_config = null`) | EKS envelope-encrypts Kubernetes API data with an AWS-owned key by default; a CMK adds $1/month and a 7–30 day deletion wait on every teardown |
| Logs | (module defaults) | flow logs and control-plane logs kept **7 days** | enough for the v0.12 flow-log check |
| `numHistoryShards` | 512 (small production) | **512** on EKS, 4 on kind | immutable; 512 keeps the EKS install shaped like production at no extra cost |
| AgentCore idle debounce | 60 s | **30 s** in the cloud, 20 s locally | shorter demo waits; Q15 fallback (b) reuses this knob (plan v0.13) |

Costs (Pricing API, us-east-1, 2026-10-31): EKS $0.10/h, m7g.large $0.0816/h, NAT $0.045/h +
EIP $0.005/h, interface endpoint $0.01/h per AZ, db.t4g.medium $0.065/h. The foundation runs at
about **$0.51/h (~$12/day)**; `make pause` (nodes to 0, RDS stopped) brings it to about
**$0.28/h**. Runbook sizing would be roughly $1.35/h.

## Consequences

- Capacity: 2 × (2 vCPU, 8 GiB), 29 pods per node with the VPC CNI. v0.11 must fit Temporal
  (frontend, history, matching, worker, UI, admin-tools), Finance × 2 and payments-mock; the
  node group can grow to 3 (`max_size`), or switch to the m6i.xlarge fallback.
- No HA claims: single NAT, single-AZ RDS. The demo never presents these as production values;
  the runbook's values stay the reference.
- PostgreSQL 16.15 is outside Temporal's tested list (16.6): recorded in `docs/versions.md` and
  the ledger (38). Temporal tests "minimum and maximum stable major versions"; a minor upgrade
  within 16 is not expected to change behaviour. If v0.11's schema job or server misbehaves on
  16.15, the fallback is the lowest available 16 minor.
- `make pause` leaves the node group's `min_size` at 0, which Terraform shows as drift until
  `make resume`.

## v0.11: what runs on the two nodes (2026-10-31)

- Temporal on EKS: frontend **2** replicas (one NLB target per node/AZ; PDB `minAvailable: 1`),
  history, matching, worker, web and admin-tools **1** each, with requests sized for 2 × m7g.large
  (`values.eks.yaml`); `numHistoryShards: 512` as decided. Finance 2 pods (spread across nodes),
  payments-mock 1. Everything fits on the 2 nodes; `max_size` 3 was not needed.
- PostgreSQL 16.15 works with Temporal 1.32.1: the chart's schema Job brought `temporal` to
  **1.19** and `temporal_visibility` to **1.14** (the runbook's targets) with no change.
- The node group's AMI release moved on (AWS published a newer AL2023 release); a plain
  `make tf-plan STACK=20-platform` now shows an in-place node group update (a node rollout).
  v0.11 applied only its one SG rule with `-target` so the nodes were not rolled mid-tag; the
  AMI update is left for a deliberate maintenance step (or v1.0 `up`).
