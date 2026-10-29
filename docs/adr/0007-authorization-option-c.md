# ADR-0007: Authorization option (c) as an accepted risk; External ID and CA kept out of Terraform state

- Status: Accepted (v0.1). Exercised from v0.9 (mTLS on kind) and v0.11/v0.12 (EKS, AgentCore).
- Runbook: 2.6, 2.7, 6.3. Plan: §3.2, appendix.

## Context

With `authorizer: default`, the default ClaimMapper reads permissions only from a
JWT, so mTLS-only Workers (AgentCore, EKS) and CLI calls without a JWT get
`PermissionDenied`. Options (runbook 2.7): (a) JWT for everyone (IdP, token refresh
for 8 h sessions), (b) a custom ClaimMapper (custom server build), (c) the noop
authorizer plus mTLS and network isolation.

## Decision

1. **Option (c).** No `authorization` block on the server. Only certs issued by the
   single in-cluster cert-manager root CA are accepted (`requireClientAuth: true`),
   and only `sg-agentcore-worker`, the bastion and the cluster can reach 7233
   (security groups, internal NLB, NetworkPolicy).
2. **Accepted risk:** any holder of a valid client cert has admin access to **both**
   namespaces, including creating or deleting Nexus endpoints and namespaces. The
   AgentCore Worker's cert could poll `agent-services` and vice versa.
3. **Hardening path, prepared now:** distinct CNs `agentcore-worker`,
   `eks-nexus-worker`, `operator`, so option (b) can later map cert subject →
   `agents:writer` / `agent-services:writer` / `temporal-system:admin`. Q17 (how
   the default Authorizer evaluates cross-namespace Nexus dispatch) is out of scope.
4. **Kept out of Terraform state:**
   - the **root CA** private key: cert-manager only (`cert-manager/temporal-root-ca`);
   - the **External ID** for the invocation role: created by
     `scripts/external-id.sh` into Secrets Manager; the CFN invocation-role stack is
     managed only by `scripts/invoke-role.sh`, which reads it at run time;
   - the RDS `temporal_user` password.
   Account IDs come from `data.aws_caller_identity`, never tfvars, and never appear
   in git (`make scrub-check`).

## Consequences

- No IdP and no custom server build for the demo.
- Restrict RBAC on the `cert-manager` namespace and enable EKS secret envelope
  encryption (v0.10/v0.11).
- The dev server (v0.1–v0.8) runs without TLS on 127.0.0.1 only; the risk above
  applies from v0.9.
