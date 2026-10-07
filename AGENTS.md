# ocish Contributor Guide

`ocish` is a Python CLI that presents OCI as a virtual, Plan 9-style namespace.
It is not a FUSE filesystem and it must not grow into a collection of
service-specific commands. The primary interface is a navigable path tree with
small, reusable verbs: `cd`, `ls`, `ll`, `cat`, `readlink`, `find`, and `rm`.

## Start Here

- Read `README.md` for the current public contract and `DESIGN-PHILOSOPHY.md`
  for the namespace model before changing behaviour.
- Treat `STATUS.md` as historical context, not a current specification.
- Inspect the affected registry, model, adapter, and validation path before
  editing. Preserve unrelated user changes.
- Use the repository environment for Python commands: `uv run ...`.

## Repository Map

- `main.py`: shell commands, navigation, node resolution, and user-visible
  behaviour.
- `inventory.py`: OCI client access, discovery, resource listings, and active
  region inventory.
- `config.py` and `config_data.py`: declarative OCI resource registry,
  hierarchy, collection, and capability configuration.
- `models.py`: namespace nodes, contexts, resource specifications, and durable
  shell-state types.
- `relationships.py`: canonical-resource and relationship-projection logic.
- `deletion.py`: preview-first deletion planning and apply behaviour.
- `completion.py`: strictly local, non-blocking namespace completion.
- `rendering.py`: stable terminal rendering.
- `static_schema.py`, `sdk_catalog.py`, and `terraform_schema_overlay.py`:
  static SDK and optional Terraform schema views.
- `tests/`: hermetic pytest suite for namespace, registry, and safety contracts.
- `validate_live_logging.py`: explicitly requested, read-only live smoke test.

## Namespace Contract

### Canonical identity and projections

- Every OCI resource has one canonical ownership path. Do not duplicate an OCI
  object as independent resource records in multiple locations.
- Ownership is administrative: tenancy -> compartment -> resource.
- Network and placement relationships are valid topology projections. A subnet
  may expose its placed compute through a projection, but that does not replace
  the instance's canonical compartment-owned identity.
- A relationship projection must resolve to the canonical target and expose a
  stable canonical path through `readlink` or the equivalent `cat` descriptor.
- Repeated relationships are directories of links; do not expose OCI array
  indexes or raw `*-ids` fields as namespace entries.

### Uniform nodes and verbs

- Every exposed node must have an intentional kind and capability: namespace
  node, collection, resource, field, terminal record, control, or projection.
- `cat .` is the stat-like contract. Keep its node kind, canonical path,
  relevant OCI identity/scope, freshness or failure state, children, and
  capabilities accurate.
- Prefer a path, field, child collection, or relationship over adding a new
  service-specific shell verb.
- Synthetic nodes are welcome when they make the namespace clearer, but once
  exposed they must behave consistently with comparable nodes.
- Visible path names use hyphens. SDK names may use underscores internally;
  retain documented aliases without creating ambiguous paths.

### Bounded discovery and query state

- Listing, searching, completion, and query views must be bounded and reveal
  whether data is live, cached, paginated, truncated, permission-limited, or
  failed. An empty result must not conceal a permission or transport failure.
- Use explicit `page`, `name`, `state`, and query-control paths rather than an
  unbounded tenancy-wide scan.
- Query controls change only shell-session state. They never mutate OCI.
- Completion must never call OCI synchronously. It may use only known local,
  cached, or already-discovered namespace information.

## OCI Boundaries and Safety

- Treat OCI calls as remote, permission-scoped, potentially billable actions.
  Keep unit validation hermetic; run live checks only when requested and scoped.
- Preserve OCI request IDs, status/code, timestamps, and useful failure
  classification when surfacing remote failures. Never log credentials, tokens,
  raw secrets, or unnecessary customer payloads.
- Never infer an ownership, placement, or relationship edge that the available
  OCI data does not establish. Represent unresolved owners explicitly.
- Mutation is preview-first. Plain `rm` must remain non-mutating; only an
  explicit `-f` or `--apply` path may issue a delete after its preflight.
- Recursive deletion must use the declared child registry, present the plan,
  block unsupported or unresolved blockers, and delete children before parents.
  Preserve stricter VCN-specific ordering and preflight rules.
- Do not widen delete support merely because a similarly named SDK method
  exists. A resource is deletable only when its identifier, lifecycle, parent
  requirements, and preflight semantics are unambiguous.

## Python Standards

- Target Python 3.12+ and follow the configured Ruff rules in `pyproject.toml`.
  Repository configuration overrides generic style preferences.
- Use type annotations for public functions, methods, and module-level
  constants. Prefer `collections.abc` interfaces for callable and collection
  annotations.
- Write straightforward, typed code with descriptive names. Prefer small pure
  helpers when they reduce state or lifecycle complexity.
- Use `pathlib.Path` for filesystem paths. Keep imports at module scope and
  grouped standard-library, third-party, then local.
- Add concise docstrings to public classes and non-obvious public functions.
  Comments explain why, compatibility, or OCI semantics—not line-by-line code.
- Raise or surface specific failures with actionable context. Do not use broad
  exception handling unless it is an intentional OCI boundary that preserves
  the original diagnostic.
- Avoid new dependencies, global state, parallel sources of truth, and broad
  refactors unless they directly satisfy the requested namespace contract.

## Change Rules

- Patch the actual control point: usually the declarative registry and its
  shared resolver/adapter, not a command-specific special case in `main.py`.
- Add services through declarative resource adapters: collection -> resource ->
  fields -> child collections -> relationships -> evidence/logs. Do not create
  a mini-CLI per OCI service.
- Any change to a path, node kind, field name, command meaning, output shape,
  delete behaviour, query control, or completion result is a compatibility
  change. Preserve existing paths and aliases where practical; otherwise make
  the migration explicit.
- Before adding a special-case branch, state the OCI/API fact that requires it
  and why the existing registry, adapter, or path model cannot represent it.
- Maintain the distinction between an authoritative live OCI response, cached
  inventory, static SDK schema, and a relationship projection.
- Preserve `cat .`, `ls`/`ll`, direct path resolution, and `find` output as
  interoperable behaviour: a discovered canonical path should be usable by
  `cd`, `cat`, and `ll`.

## Tests and Verification

- Add or update focused regression coverage for a behaviour change. Test the
  user-visible path and command outcome, not only a private helper call.
- Cover representative success, failure, and boundary cases. For hierarchy
  changes, test canonical ownership and each relationship projection affected.
- Do not weaken a validation or test to accept an incorrect namespace shape.
- Run the narrowest relevant test while iterating. For namespace, registry,
  hierarchy, completion, or core-path changes, run:

  ```bash
  uv run pytest
  ```

- For a formatting/lint-only change, run the relevant configured Ruff check if
  available in the environment. Do not claim it passed if it was not run.
- Run `validate_live_logging.py --compartment-id <ocid>` only for a requested,
  read-only live validation with an explicit compartment scope. Never use live
  validation as a substitute for hermetic regression coverage.

## Completion Criteria

Before declaring an implementation complete, confirm:

- the changed behaviour matches the documented namespace and safety contract;
- canonical ownership, relationship projections, and failure semantics remain
  coherent;
- relevant focused validation passed, or the exact blocker is stated;
- no unrelated changes or generated `ocish.egg-info/` output are included;
- the final report names changed files, validation performed, and any remaining
  limitation.
