# OCI Nav Shell

`ocish` is a virtual Plan 9-style OCI namespace. It is a CLI, not a FUSE mount.

```bash
python3 -m pip install .
ocish
```

The command can then run from any directory. During development, use
`python3 main.py` from the repository root.

## Quick Start

```text
ocish
ls                         # child compartments and available collections
cd <compartment-path>/core/instances
ll
cd bastion
cat .                      # stat metadata plus raw OCI payload under data
ll                         # fields, child collections, relationships, logs
cat availability-domain
pwd
```

Use `/` or `~` for the tenancy root, `..` to move up, and `cd -` to toggle to
the previous location. A path may start with a subscribed region:

```text
cd /<region>/<tenancy>/<compartment-path>/core/instances/<instance>
```

## Namespace

```text
/<region>/<tenancy>/<compartment-path>/<domain>/<resource-type>/<resource-name>
```

`/oci` is a separate static SDK-schema tree. It makes no OCI calls and is not
part of the tenancy namespace. It is discovered from every installed OCI SDK
service package, not from the smaller set of live services implemented by
`ocish`:

```text
cd /oci/core/instances
ll
cat attributes
cat operations
cat metadata
find .                     # recursively list SDK-native resource paths
```

Optional Terraform-provider coverage is disabled by default. To enrich the
static schema from a local provider checkout, set
`OCISH_TERRAFORM_PROVIDER_OCI_ROOT` to that checkout's root directory.

Examples:

```text
/<region>/<tenancy>/<compartment-path>/core/instances/<instance>
/<region>/<tenancy>/<compartment-path>/logging/log-groups/<group>/logs/<log>
/<region>/<tenancy>/<compartment-path>/load-balancer/load-balancers/<load-balancer>
```

At a compartment, `ls` shows immediate navigation targets: child compartments
and directly navigable resource collections that are present there (for
example, `core.vcns`). `ll` adds each entry's kind and collection count.
`identity.compartments` appears only when the compartment has child
compartments. Domains remain explicit paths: `core`, `dns`, `identity`, `orm`,
`containerengine`, `generative-ai`, `generative-ai-agent`, `logging`, and
`load-balancer`, and `network-firewall`.

Generative AI resources are compartment-scoped. For example:

```text
cd generative-ai/projects
cd generative-ai-agent/agents
cd <agent>/tools
cd <agent>/agent-endpoints
```

Connector file syncs and ingestion logs are beneath their connector; Agent
knowledge bases expose data sources and their ingestion jobs. Work requests in
either Generative AI domain expose logs and errors beneath the work request.

SDK names use underscores internally; visible namespace names use hyphens.
For example, the public path is `load-balancer/load-balancers`, while
`load_balancer.load_balancers` remains an accepted input alias.

Each resource has fields, `actions`, child collections, and a lazy `logs/`
relationship projection. Canonical ownership and relationship views are
distinct. For example, a Security List is compartment-owned at
`core/security-lists/<name>` and can also appear below its related VCN.

Virtual symlinks are operational traversal only.  A resource's compartment is
ownership metadata: it remains in `cat .`, but is not a `compartment` symlink,
because the current path already expresses that containment and following it
adds no resource-local operation.

A single relationship is a symlink (`vcn`). Repeated relationships are a
directory named for the relation (`backend-nsgs/`), containing links named for
the target resources; OCI array positions and `*-ids` field names are never
exposed as namespace entries.

Use `readlink <relationship>` on a resource, or inside a relationship
directory, to print the target's canonical path. `cat <relationship>` returns
the same target as a symlink descriptor with `canonical_path`.

Logging is canonical under `logging/log-groups/<group>/logs/<log>`. A resource's
`logs/` directory is a cached projection of log objects whose source is that
resource or one of its discovered parent relationships; `canonical-path` shows
the canonical logging location.

OKE clusters expose `work-requests/` as a cluster-scoped operational projection.
Each work request has `work-request-errors/` and `work-request-logs/` children;
the cluster's creation request is also available as the
`creation-work-request` relationship when present.

At the tenancy root, `~/logging/log-groups` is a cached, tenancy-wide index of
accessible log groups. Entering a group returns to its owning compartment's
canonical `logging/log-groups/<group>` path; a child compartment's `logging/`
domain continues to list only resources owned there.

Network Firewall is available under `network-firewall/`:

```text
cd network-firewall/network-firewalls
cd <firewall>
cat .
cd network-firewall/network-firewall-policies/<policy>/security-rules
```

Firewall policies expose their address/application/service/URL lists and rule
collections; Network Firewall work requests expose logs and errors. When a
subnet blocker VNIC matches a firewall's subnet and IPv4 address, its blocker
record identifies that firewall and its canonical owner path.

Each configured log exposes `entries/`, a live Logging Search projection from
the preceding 14 days. It is navigable through query views: `entries/page/2`,
`entries/contains/<text>`, `entries/where/<field>/<value>`,
`entries/since/<timestamp>`, and `entries/until/<timestamp>`. Views compose,
so a filtered view can contain `page/2`. Each page has up to 50 entries (page
1 through 20). Entries have ISO-8601 names and are terminal JSON records; use
`cat <entry>` to inspect the full payload.

## Time and Query Data

`cat .` in a query collection shows its provider, accepted controls, and the
current per-session values. `ll` shows controls and, once required controls
are set, terminal result records. `cat <control>` reads a control file;
`cat <record>` returns the raw OCI payload.

| Path | Required controls | Example |
| --- | --- | --- |
| `logging/.../entries` | None | `write contains error` |
| `audit/events` | None | `write since 2026-09-20T00:00:00Z` |
| `monitoring/metrics` | `namespace`, `query` | `write query 'CpuUtilization[1m].mean()'` |
| `apm/apm-domains/<domain>/query` | `query` | `write query '<APM query>'` |
| `log-analytics/queries` | `query` | `write query '<Log Analytics query>'` |

Common controls are `since`, `until`, and `page`; `ll` lists provider-specific
ones. Logging retains OCI Logging Search's 14-day maximum, 50 records per
page, and 20-page bound.

Logging supports both path and control-file forms:

```text
cd logging/log-groups/central/logs/flow/entries
cd contains/error/page/2
cat 2026-09-16T12:59:10.777Z-11

cd /<region>/<tenancy>/<compartment>/logging/log-groups/central/logs/flow/entries
write contains error
write page 2
ll
```

`write` changes only this `ocish` session. It is not an OCI mutation.

Query results are terminal records: `cat <record>` returns the same stat
envelope as other nodes, including the active provider, local control values,
page bound, and the provider's raw record under `data`.

## Finding and Relationships

```text
find . instances
find load-balancer public
cd core/vcns/network-a
ll
cd relationships
ll
```

Each OCI resource has one canonical path. Relationship entries, including
`logs/`, are projections to that same object rather than copies.

Subnets expose `blockers/` as an operational projection. It currently lists
non-Compute service VNICs that prevent subnet deletion; `cat <blocker>` shows
the VNIC's private IP, hostname label, and raw OCI payload. An unresolved
service owner is reported explicitly rather than inferred.

`find` prints absolute canonical paths that can be passed directly to `cd`,
`cat`, or `ll`. Duplicate resource names in one collection receive a stable
`@<id-suffix>` leaf so every result remains addressable.

Large ordinary collections are bounded filesystem views. `ls` exposes `page/`,
`name/`, and `state/`; compose them as paths such as
`instances/page/2/name/api/state/RUNNING`. A normal OCI-backed page contains at
most 100 entries and page numbers are bounded to 1–20. `cat .` reports the
last listing state (`live`, `provider-unpaged`, `permission-limited`, or
`error`), returned count, truncation, and active local filters. `name` and
`state` filter the selected page (page 1 when omitted); they do not trigger a
tenancy-wide scan.

`cat .` is the namespace stat record. It consistently reports node kind,
canonical path, OCI identity when applicable, region/compartment scope,
freshness state, available children, capabilities, and query controls. The
original OCI or virtual-node payload remains under `data`.

Every registry resource also declares an adapter: `resource-tree` for ordinary
OCI resources, `bounded-time-query` for time/query evidence, or
`hierarchical-content` for content trees such as Object Storage. New services
extend this registry contract rather than adding service-specific shell verbs.

When an OCI call fails, the current node's `cat .` also includes a `failure`
record with its classification (`permission-denied`, `region-not-subscribed`,
`service-unavailable`, `throttled`, or `error`), OCI status/code, request ID,
message, and UTC timestamp. Collection `listing` state separately distinguishes
an empty page from `permission-limited` or `error`.

## Object Storage

Object Storage is a hierarchical-content provider:

```text
cd object-storage/buckets
cd <bucket>/objects
ll                         # object-prefix directories and object metadata
cd reports                 # descend through a prefix
cat summary.json           # raw object metadata
cat summary.json/content   # small UTF-8 text/JSON/XML bodies only
```

`content` never blindly prints data. It rejects binary MIME types, invalid
UTF-8, and objects larger than 64 KiB; use `cat <object>` to inspect metadata
when a body is guarded. Object listing follows OCI `nextStartWith` pagination
up to 20,000 entries; descend into a prefix when a bucket directory is larger.

OCI Artifacts and DevOps repositories are mounted through the ordinary
declarative tree: `artifacts/repositories/<repository>/generic-artifacts` and
`devops/projects/<project>/repositories`. Container images are available at
`artifacts/container-images`. They are metadata collections; no artifact or
image body is downloaded implicitly.

## Commands

- `ls [OPTION]... [path]`; `ll [path]` is `ls --long [path]`
- `cd <path>`, `cd ..`, `cd /<region>`
- `cat .`, `cat <field>`, `cat <path>`
- `readlink <relationship>`
- `stat [.]` (alias for the current node's structured `cat .`)
- `tree [.]` (one cached namespace level; never discovers remotely)
- `head|tail [-n N] <cached-terminal-record>`
- `grep <text>` (cached terminal records only)
- `write <control> <value>` (time/query collection controls only)
- `pwd`
- `find .` (all searchable types in the current compartment; current service only when run inside a service)
- `find <type> [name] | find <path> <type> [name]`
- `rm [-r] [-f|--apply] <path>`

`rm` remains preview-first and supports only selected resource types.
For Network Firewalls, `rm <firewall>` performs a read-only lifecycle and
resource-scoped work-request preflight; `rm -f <firewall>` or
`rm --apply <firewall>` deletes only when that preflight is clear.

For other navigable resources, `rm` discovers a generic capability only when
the installed OCI client exposes an unambiguous `delete_<resource>(<id>)`
operation. Plain `rm` performs a direct-read preflight where a getter is known;
`rm -f` or `rm --apply` is always required to issue the delete. Resources with
parent-only or multi-identifier delete APIs remain explicitly unsupported.

`rm -r` uses the declared child-collection registry for any resource type: it
previews child deletes first, blocks on an unsupported child, and deletes
children before their parent on `--apply`. VCNs retain their stricter
network-specific order and blocker preflight. A VCN plan scans accessible
compartments for supported external consumers and prints each recognized
blocker's canonical path; it marks the scan incomplete when a compartment or
service could not be inspected.

## Completion

Completion never calls OCI. Use `completion off`, `completion static`,
`completion cached`, or `completion catalog` (default), and `help completion`
for the contract. Catalog mode builds an active-region OCI Search snapshot in a
background worker and refreshes it every minute.

Completion is a namespace cache: `ls`/`ll` contribute observed collection and
resource entries, resource inspection contributes observed relationship names,
and `find` contributes its canonical result paths. Pressing Tab never resolves
or lists an unseen nested OCI path.

| Input | Completion source |
| --- | --- |
| `ls`, `ll`, `cd`, `cat` with a domain/type | Active-compartment catalog (after its background refresh) |
| `~/...` or `/...` | Known current compartment prefix plus local specs |
| `identity.users/...` or `core/instances/...` | Resource names cached after listing that collection |
| Inside a resource | Fields, child collections, `logs`, and virtual symlinks |
| `find`, `rm`, unseen compartments, unseen resource names | Not completed |

## Validation

```bash
uv run pytest
```

The audit validates registered resource resolution, hierarchy ownership versus
relationship projections, typed path resolution, registry-derived completion,
and the universal `logs/` projection.

For read-only live Logging coverage in one compartment:

```bash
python3 validate_live_logging.py --compartment-id <compartment-ocid>
```
