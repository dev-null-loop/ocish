# `ocish` implementation backlog

`ocish` already has canonical `find` paths, stable duplicate-name handling, a
uniform stat record, bounded collection paging/filtering, structured OCI
failure records, non-blocking cached completion, bounded query directories,
and the read affordances (`readlink`, `head`, `tail`, `grep`, `stat`, and
`tree`) described by earlier planning notes.

The remaining work should deepen the namespace contract rather than add a
parallel menu-driven CLI.

## 1. Broaden relationship coverage

The generic relationship mechanism is implemented: direct and indirect
references become canonical symlinks or relationship directories, and
`readlink` returns the canonical target path.  What remains is coverage:
declare and validate the important OCI relationships that cannot be inferred
reliably from generic payload OCID references.

Priorities include:

- canonical ownership versus placement/topology projections;
- complete relationships for the services already in the live namespace;
- relationships required by recursive deletion preflight; and
- explicit unresolved relationship state rather than guessed containment.

Keep one canonical resource path. A relationship projection must always point
back to it; it must never create a second independently managed copy.

## 2. Broaden live service coverage through declarative adapters

Add services as declared resource adapters:

```text
collection -> resource -> fields -> child collections -> relationships -> evidence/logs
```

Do not add a service-specific mini-CLI. Reuse the existing adapter classes:

- `resource-tree` for ordinary OCI resources;
- `bounded-time-query` for time/query evidence; and
- `hierarchical-content` for content trees such as Object Storage.

Missing live service coverage to prioritize: File Storage, Lustre,
Notifications/ONS, and KMS/Vault. Add a readable namespace and canonical
relationships before considering any mutations.

## 3. Read-only operational coverage

Ordinary OCI resource navigation is already broadly available. The following
numbered work items are the remaining read-only diagnostics, reports, and
service views from `oci_cli_ops` that need namespace-native equivalents:

1. [x] File Storage: filesystems, mount targets, exports, snapshots, and their
   work requests.
2. Lustre: filesystems, Object Storage links, and Lustre work requests.
3. Notifications/ONS: topics, subscriptions, and event rules.
4. KMS/Vault: vault, key, and encryption details.
5. Identity Domains: domain-scoped users, groups, dynamic groups, identity
   providers, and policy search/validation evidence.
6. Compute/GPU reports: cliques and fabric membership; firmware and
   unprovisioned-host reports; compute-host health, impacted components, and
   recycle status; capacity/RDMA topology; and pool/cluster-network topology.
7. Instance evidence: serial-console history, decoded user data, cloud-init
   comparisons, and instances-not-in-Kubernetes reporting.
8. Kubernetes diagnostics: GPU Operator/DRA health; storage, network, GPU,
   XID, RDMA/NCCL, NVMe, NVIDIA, SOS, and provisioning checks. These require a
   separately designed, read-only kubectl/helm adapter.
9. Network analysis: NSG/security-list port search, DRG connectivity maps, and
   cross-region DRG topology.
10. Operations evidence: maintenance events and timelines, announcements,
    fleet exports, GPU capacity/shape availability, quota/limit aggregation,
    Resource Manager Terraform state and job logs, and Object Storage
    private-endpoint DNS/port/rule diagnostics.

Implement each as a bounded collection, terminal record set, relationship
projection, or time/query adapter—not as a second menu-driven reporting UI.

## 4. Guarded operational workflows

`oci_cli_ops` has a much larger operations-console surface than `ocish`.
`ocish` already exposes many of the underlying OCI resources as navigable,
readable collections, but its executable contract is intentionally small:
navigation, inspection, bounded queries, relationship traversal, and
preview-first deletion. `/oci/.../operations` is static SDK schema, not an
action runner.

Do not add a parallel collection of service-specific shell commands. When an
operation belongs in `ocish`, model it as an explicit, resource-local,
guarded action beneath the canonical resource path, with readable inputs,
preview where meaningful, and an explicit apply/confirmation boundary.

Candidate work:

- **Compute actions and evidence:** lifecycle actions beyond deletion; instance
  agent/run-command; boot-volume replacement; console-history and cloud-init
  evidence; instance configuration/pool/cluster workflows; GPU fabric,
  firmware, and memory-cluster operations.
- **OKE and GPU diagnostics:** an intentionally separate, opt-in execution
  adapter for kubectl/helm-backed node diagnostics and remediation (GPU/DRA,
  XID, RDMA/NCCL, storage/network checks, daemon sets, tolerations, and
  kubelet recovery). These must not run during completion or ordinary
  navigation.
- **Network operations:** declarative, previewable rule changes; DRG
  connectivity and cross-region evidence; and Object Storage private-endpoint
  workflows. Preserve the existing VCN/subnet/consumer projections rather
  than creating a separate topology CLI.
- **Operations workflows:** maintenance-event rescheduling and evidence,
  Resource Manager job execution, service-limit/capacity reports, and
  constrained fleet/export reports. Audit remains a bounded query directory,
  not an interactive report menu.
- **Identity-domain administration:** domain user/group/dynamic-group/IdP
  workflows and policy validation/search, only if they can be represented as
  safe resource-local operations rather than a second management UI.

Every mutation needs an OCI API-specific safety contract: declared input
files/fields, a dry-run or preview whenever the API permits it, explicit
`--apply`-equivalent confirmation, work-request tracking where applicable,
and hermetic tests for the user-visible path and failure semantics.
