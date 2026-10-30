# ADR-0007: Authorization option (c) as an accepted risk; External ID and CA kept out of Terraform state

- Status: Accepted (v0.1). Exercised on kind at v0.9 (section below); EKS and AgentCore at v0.11/v0.12.
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

## v0.9: exercised on kind (mTLS everywhere)

What the decision looks like once it runs (chart temporal-1.7.0, server 1.32.1,
cert-manager v1.21.3; `scripts/verify/v0.9-temporal-on-kind-mtls.sh`):

- **One root, one issuer, exactly as runbook 2.6.** `selfsigned-bootstrap` (SelfSigned
  ClusterIssuer) issues `cert-manager/temporal-root-ca` (ECDSA P-256, 10 years), which
  backs the `temporal-ca` CA ClusterIssuer. Every server and client certificate comes from
  `temporal-ca`, so the chart's single `frontend.server.clientCaFiles` entry
  (`/etc/temporal/tls/frontend/ca.crt`, the root) trusts all clients (runbook 2.5 option 1).
  No Terraform, no file on disk outside `.certs/` (gitignored), no root key outside the
  cluster.
- **Distinct CNs now** (the option (b) hardening path): `temporal-ui`, `operator`,
  `eks-nexus-worker`, `agentcore-worker`, `laptop-support`. Each client certificate has
  only `client auth`; on kind each laptop Worker presents its own (the Makefile sets
  `TEMPORAL_TLS_CLIENT_CERT_PATH` / `_KEY_PATH` per target on `TEMPORAL_PROFILE=kind`).
- **Rejected:** a plaintext client, a TLS client without a certificate, a client
  certificate from a foreign CA (even with CN `operator`), and the operator certificate
  when the client expects the wrong server name. Accepted: any certificate from
  `temporal-ca`, for **both** namespaces: the accepted risk in decision 2 is real and
  unchanged.
- **Network isolation is part of the control** (decision 1): a NetworkPolicy admits
  7233 on the frontend pods only from the `temporal` namespace and from pods in
  `temporal-workers` labelled `temporal.io/client: "true"`; an unlabelled pod (any
  namespace) cannot connect. The Finance pods' egress is limited to the frontend, DNS,
  443 and payments-mock. kindnet enforces both (ledger 36).
- **New fact for the accepted risk (ledger 34):** chart 1.7.0 mounts the **frontend
  server Secret** (`temporal-frontend-tls`, private key included) into its admintools
  Deployment and hook Jobs and presents it as their client certificate. So the frontend
  certificate needs `client auth` as well (a deviation from runbook 2.6's server-only
  usages, the same choice as the chart's own CI values), and anyone who can exec into
  admintools holds a key the frontend trusts as a client. Under option (c) that is no
  new privilege (admintools is already admin), but RBAC on `exec` in the `temporal`
  namespace belongs to the same control as RBAC on `cert-manager` Secrets.
- **Rotation:** cert-manager re-issues; the server re-reads its own certificate without
  a restart (Q10); the Python SDK keeps the certificate it read at connect (Q18), so a
  renewed Worker certificate reaches a Worker on its next start (rollout restart, or
  Reloader) while the old one stays valid until it expires.
