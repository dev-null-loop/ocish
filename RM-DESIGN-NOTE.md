# `rm` Design Note

Date: 2026-09-02

`ocish` can support an `rm` command without breaking the filesystem model, but only if deletion semantics stay strict and conservative.

## Fit

- `rm <resource-path>` is a natural path-based delete operation.
- `rm <collection-path>` should not delete anything.
- `rm <domain/type-path>` should not delete anything.
- `rm <compartment-path>` should not be supported initially.
- A valid `rm` target should resolve to exactly one resource node.

## Safety Model

- Plain `rm <path>` should be preview-only.
- Real deletion should require an explicit apply form such as `rm -f <path>` or `rm --apply <path>`.
- The preview should print the resolved resource type, display name, OCID, and delete API method.
- Ambiguous synthetic names should fail and require the exact resolved path.

## OCI-Specific Constraints

- Many OCI deletions are asynchronous.
- Some resources require dependency cleanup before deletion.
- Some delete APIs require extra parameters or behave differently across services.
- `ocish` should not pretend OCI deletes are as simple or uniform as local filesystem deletes.

## Recommended Rollout

1. Implement preview-only `rm` first.
2. Add a small allowlist of supported resource types.
3. Add real delete execution behind `-f` or `--apply`.
4. Add optional wait/poll behavior only after the base contract is stable.

## First Implementation

The first implemented `rm` target in `ocish` should be `core.custom-images`.

- path shape: `rm <compartment-path>/custom-images/<image>`
- plain `rm` prints a preview only
- `rm -f <path>` or `rm --apply <path>` executes `ComputeClient.delete_image`
- `rm` should not operate on `core.images` broadly, because that collection also contains platform images
- collection, domain/type, and compartment paths remain non-deletable

`core.vcns` is also supported for direct delete semantics only:

- path shape: `rm <compartment-path>/vcns/<vcn>`
- plain `rm` prints a preview only
- `rm -f <path>` or `rm --apply <path>` executes `VirtualNetworkClient.delete_vcn`
- this is not recursive teardown
- if OCI rejects due to dependencies, `ocish` should surface that directly

Recursive VCN teardown now exists behind `rm -r`, but only as a VCN-specific planner/apply flow.

- `rm -r <vcn-path>` previews the discovered plan
- `rm -r -f <vcn-path>` applies the ordered core-network teardown
- same-compartment blockers such as instances, load balancers, OKE resources, instance pools, and cluster networks are detected and stop apply

## Initial Scope

Reasonable first candidates:

- internet gateways
- NAT gateways
- service gateways
- local peering gateways
- network security groups
- route tables
- subnets

Resources that should wait until later:

- compartments
- recursive subtree deletion
- broad multi-resource deletes
- anything that depends on complicated detach/teardown choreography

## Remaining Non-Goals

- no delete on synthetic nodes
- no delete by ambiguous display name
- no silent fire-and-forget deletion without clear status
