# OCI Nav Shell Status

Date: 2026-09-01

> Historical snapshot, retained as an archive rather than a behavior
> specification. It predates the canonical domain namespace,
> ownership/projection model, Logging, and Load Balancer support. For current
> behavior, see `README.md` and `DESIGN-PHILOSOPHY.md`.

## Design Direction

- `ocish` follows a Plan 9-style namespace model.
- The shell prefers a small path-oriented command set (`cd`, `ls`, `cat`, `pwd`, `find`) over service-specific verbs.
- Synthetic directories such as `region` and `service` are intentional namespace mounts, not ad hoc command features.
- Resource and child-collection contexts are designed to feel like traversing mounted subtrees.
- `rm` now exists as a path-based delete verb with preview-by-default semantics.

## Mutation

- `rm <resource-path>` currently supports preview and apply for `core.custom-images` and direct `core.vcns`.
- Plain `rm` is preview-only.
- `rm -f <path>` and `rm --apply <path>` execute deletion.
- Collection, service/type, and compartment paths are not deletable.
- `rm -r <vcn-path>` now previews a dependency-aware core-network teardown plan for VCNs.
- `rm -r -f <vcn-path>` applies that plan only when no discovered blockers remain.

## Core

- `core` is finished for listing/navigation scope.
- Public `core` taxonomy is normalized to `69` top-level resources.
- Registry coverage comes from the wired OCI Python SDK Core clients:
  - `VirtualNetworkClient`
  - `ComputeClient`
  - `ComputeManagementClient`
  - `BlockstorageClient`
- Raw SDK child/helper collections remain available internally for context navigation, but are not shown as top-level `ls service/core` entries.
- `ls service/core` and `ls -l service/core` are working.
- `ls <type>` works for runnable Core list APIs.
- `ls -l <type>` works for all runnable Core list APIs.
- `ls region` shows subscribed regions and `cd region/<name>` switches region view in-session.
- `find` currently uses `find <type> | find <name> <type> | find <name> <type> <path>`.
- Typed top-level `find` searches walk the current compartment subtree through OCI Search.
- High-value curated long views exist for:
  - `core.instances`
  - `core.vcns`
  - `core.subnets`
  - `core.images`
  - `core.custom-images`
  - `core.platform-images`
  - `core.volumes`
  - `core.boot_volumes`
  - `core.volume_groups`
  - `core.volume_backups`
  - `core.boot_volume_backups`
  - `core.volume_group_backups`
  - `core.block_volume_replicas`
  - `core.boot_volume_replicas`
  - `core.dhcp_options`
  - `core.services`
  - `core.cross_connects`
  - `core.cross_connect_groups`
  - `core.public_ips`
  - `core.public_ip_pools`
  - `core.remote_peering_connections`
- Other runnable Core types use the shared generic long-column heuristic.
- Additional curated child long views now exist for:
  - `core.private_ips`
  - `core.vnic_attachments`
  - `core.volume_attachments`
  - `core.boot_volume_attachments`
  - `core.drg_attachments`
  - `core.route-rules`
  - `core.security-list-rules`
  - `core.drg_route_rules`
  - `core.network_security_group_security_rules`
  - `containerengine.work_request_logs`
  - `containerengine.work_request_errors`
- Hyphen/underscore aliases are accepted.
- OCI SDK `**kwargs` list operations are handled via docstring-derived accepted parameters, so filtered operations such as `network-security-groups` resolve correctly.
- `cat .` shows the underlying OCI JSON payload, including tags.
- Long views resolve many OCI IDs to cached names when available and fall back to OCI search plus direct OCI `get_*` lookups.
- Direct `get_*` fallback coverage now includes public IPs, cross-connects, DHCP options, remote peering connections, volume groups/backups, and replicas.
- `cat .` and `cat <type>/<name>` recursively resolve OCI IDs inside JSON payloads, including lists of IDs.

## Not In Scope For “Finished”

- Parent-resource list APIs that need extra identifiers are intentionally not runnable from compartment context alone.
- Not every Core resource has a bespoke long view yet.
- Some generic views still show OCIDs where a resolved related resource name would be better.

## Resource Context

- Resource-instance context is now supported with `cd <type>/<name>`.
- The prompt and `pwd` show the full locator as `oci:/path...@<region>`.
- Changing region preserves the compartment path and clears any active resource context.
- Navigation is intentionally Unix-like; there are no `up` or `back` aliases.
- Hierarchy rule is now conservative:
  - VCN = topology container
  - subnet = address container
  - route table = route-rules container
  - NSG = security-rules container
  - security list = security-list-rules container
  - do not flatten detailed descendants upward when a narrower parent is more natural
- Context-aware child collection navigation is wired for the main parent-resource surfaces in `core`, `dns`, `containerengine`, and `identity`, including:
  - VCNs, subnets, instances, images, DRGs, route distributions/tables, NSGs, virtual circuits, IPSec connections/tunnels, CPEs, public IP pools, cross-connect groups, FastConnect provider services, instance pools, dedicated VM hosts, capacity reservations/topologies, app catalog listings, BYOIP ranges, cluster networks
  - DNS resolvers and zones
  - OKE clusters, virtual node pools, and work requests
  - IAM availability domains, groups, IAM work requests, tag namespaces, and users
- `ls` lists child collections in context.
- `ls -l` shows child collection -> resource type mapping.
- `ls <child>` runs relative to the selected parent resource.
- `help context` prints the resource-context command summary.
- `cat` is now the preferred content verb:
  - `cat .` prints the current object JSON
  - `cat <path>` prints any resolved node JSON, including collection and service/type nodes
  - `cat <path>/.` remains accepted as a compatibility form
  - `cat <child>/<name>` prints one child object JSON

## Container Engine

- Public `containerengine` taxonomy currently exposes `11` types.
- All exposed `containerengine` types are now covered at the navigation layer:
  - directly listable from compartment context, or
  - reachable through cluster / virtual-node-pool / work-request context
- Curated long views now exist for:
  - `containerengine.clusters`
  - `containerengine.node_pools`
  - `containerengine.virtual_node_pools`
  - `containerengine.addon_options`
  - `containerengine.addons`
  - `containerengine.pod_shapes`
  - `containerengine.virtual_nodes`
  - `containerengine.work_requests`
  - `containerengine.work_request_logs`
  - `containerengine.work_request_errors`
  - `containerengine.workload_mappings`

## DNS

- Public `dns` taxonomy currently exposes `7` top-level resources.
- DNS child collections currently exposed through resource context:
  - `resolver-endpoints` under `dns.resolvers`
  - `zone-records` under `dns.zones`
- Curated long views now exist for:
  - `dns.zones`
  - `dns.zone_transfer_servers`
  - `dns.resolvers`
  - `dns.resolver_endpoints`
  - `dns.views`
  - `dns.tsig_keys`
  - `dns.steering_policies`
  - `dns.steering_policy_attachments`
  - `dns.zone-records`
- Live DNS listing depends on tenancy authorization for the DNS service in the active region.

## Identity

- Public `identity` taxonomy currently exposes `12` top-level resources.
- Exposed parent-resource contexts currently include:
  - `identity.availability_domains`
  - `identity.groups`
  - `identity.iam_work_requests`
  - `identity.tag_namespaces`
  - `identity.users`
- Curated long views now exist for:
  - `identity.compartments`
  - `identity.users`
  - `identity.groups`
  - `identity.dynamic_groups`
  - `identity.policies`
  - `identity.tag_namespaces`
  - `identity.tags`
  - `identity.network_sources`
  - `identity.domains`
  - `identity.availability_domains`
  - `identity.fault_domains`
  - `identity.regions`
  - `identity.region_subscriptions`
  - `identity.iam_work_requests`
  - `identity.iam_work_request_logs`
  - `identity.iam_work_request_errors`
  - `identity.user_group_memberships`
  - `identity.api_keys`
  - `identity.auth_tokens`
  - `identity.customer_secret_keys`
  - `identity.db_credentials`
  - `identity.mfa_totp_devices`
  - `identity.o_auth_client_credentials`
  - `identity.smtp_credentials`
  - `identity.swift_passwords`

## ORM

- Public `orm` taxonomy currently exposes `9` top-level resources.
- Registry coverage comes from the wired OCI Python SDK `ResourceManagerClient`.
- Exposed parent-resource contexts currently include:
  - `orm.stacks`
  - `orm.jobs`
  - `orm.work_requests`
- Curated long views now exist for:
  - `orm.stacks`
  - `orm.jobs`
  - `orm.private_endpoints`
  - `orm.templates`
  - `orm.configuration_source_providers`
  - `orm.work_requests`
  - `orm.template_categories`
  - `orm.terraform_versions`
  - `orm.resource_discovery_services`
  - `orm.stack_associated_resources`
  - `orm.stack_resource_drift_details`
  - `orm.job_associated_resources`
  - `orm.job_outputs`
  - `orm.work_request_logs`
  - `orm.work_request_errors`
