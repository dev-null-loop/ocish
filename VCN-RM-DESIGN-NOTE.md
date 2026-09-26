# `rm` Design Note for VCNs

Date: 2026-09-02

VCNs are the main case where the `ocish` filesystem model meets OCI dependency graphs directly.

## Problem

`rm <compartment-path>/vcns/<vcn>` looks like a normal path-based delete, but a VCN is a topology container with many attached resources:

- subnets
- route tables
- security lists
- network security groups
- internet/NAT/service/local peering gateways
- DRG attachments
- other service-managed dependents that may exist in or around the VCN

So VCN deletion is not a leaf-resource delete. It is a graph teardown problem.

## Recommended Semantics

- `rm <compartment-path>/vcns/<vcn>`
  Preview only.
  Show the resolved VCN plus the fact that this resource is a container with likely blockers.

- `rm -f <compartment-path>/vcns/<vcn>`
  Attempt only the direct OCI delete for the VCN itself.
  Do not recurse.
  If OCI rejects because the VCN is still in use, surface that error plainly.

- `rm -r <compartment-path>/vcns/<vcn>`
  Builds a dependency-aware delete plan.
  It does not immediately recurse on plain preview.

- `rm -r -f <compartment-path>/vcns/<vcn>`
  Applies the ordered core-network teardown only when the discovered blockers list is empty.

## SDK-Oriented Deletion Order

Using the OCI Python SDK, the practical VCN teardown order should be dependency-first rather than type-alphabetical.

1. Delete non-Core consumers inside the VCN first.
   Examples:
   - instances / VNIC users
   - OKE clusters and node pools
   - load balancers
   - anything else still consuming the VCN's subnets, NSGs, or private IPs

2. Delete subnet-contained networking leaves.
   Examples:
   - explicit private IPs
   - public IPs that still need explicit cleanup
   - VLANs
   - VTAPs

3. Delete subnets.
   `delete_subnet` is only allowed when subnet consumers are already gone.

4. Delete VCN-level attachments and gateways.
   Examples:
   - DRG attachments
   - local peering gateways
   - internet gateways
   - NAT gateways
   - service gateways

5. Delete VCN-scoped policy/topology objects that are no longer referenced.
   Examples:
   - network security groups
   - route tables
   - security lists
   - DHCP options

6. Delete the VCN itself.

Important detail:

- route rules and security-list rules are not separate delete resources
- they are mutated through their parent route table or security list
- so they should not appear as standalone teardown steps

## Why `rm -r` Must Be Different

For local filesystems, recursive removal is mostly tree traversal.
For OCI networking, recursive removal is ordered teardown with dependency rules, partial failures, async deletes, and external actors.

The implemented `rm -r` for VCNs should:

1. Discover dependent resources.
2. Build an ordered plan.
3. Show the plan before mutation.
4. Require a second explicit apply step.
5. Report partial progress and failures clearly.

## Suggested Future UX

Preview:

```text
$ rm -r <compartment-path>/vcns/<vcn>
would build delete plan:
path: /<tenancy>/<compartment-path>/vcns/<vcn>
type: core.vcns
name: dev
id: ocid1.vcn...
children:
  subnets: 3
  route-tables: 4
  security-lists: 2
  network-security-groups: 5
  internet-gateways: 1
  nat-gateways: 1
```

Apply:

```text
$ rm -r --apply <compartment-path>/vcns/<vcn>
delete plan started:
plan_id: ...
```

## First Principle

For large OCI container resources, `ocish` should stay path-oriented but must not pretend the target is a simple file.

Direct `rm` is about one resolved node.
Recursive `rm` for VCNs, if it exists at all, should be a planner-backed teardown workflow.

## Cross-Compartment Caveat

A VCN path lives in one compartment, but dependent resources may be spread across multiple compartments.

Examples:

- load balancers attached to a subnet in the VCN may live in another compartment
- DRG attachments may reference networking objects across compartment boundaries
- service-managed resources may consume subnets, private IPs, or NSGs without living in the VCN's compartment

This means a VCN teardown planner cannot assume:

- list in the VCN's compartment
- delete everything found there
- then delete the VCN

Instead, the planner should treat the VCN OCID as the anchor and discover dependents by reference, not only by compartment.

Practical implication for `ocish`:

- direct `rm -f` on a VCN should still attempt only the direct `delete_vcn`
- `rm -r` currently detects same-compartment blockers in the implemented planner
- cross-compartment blocker discovery remains incomplete and must be treated as a known limitation
- a plan should clearly label which blockers are in-compartment versus cross-compartment
