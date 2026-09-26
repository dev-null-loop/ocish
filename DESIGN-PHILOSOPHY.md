# Design Philosophy

Date: 2026-09-02

This note captures the Plan 9 design ideas most relevant to `ocish`.

It is not a complete history of Plan 9. It is a working philosophy note for how `ocish` should think about interfaces, paths, state, and composition.

## Core Orientation

The central Plan 9 instinct is to reduce many different kinds of system interaction into a small number of uniform ideas.

The most important of those ideas are:

- represent services as file trees
- make objects reachable by path
- keep the verb set small
- prefer composition over special-purpose interfaces
- make state inspectable

In `ocish`, that becomes:

- OCI domains appear as explicit path components
- OCI resources are named by paths
- `cd`, `ls`, `cat`, `find`, and `rm` do most of the work
- resource-specific complexity should be pushed underneath a uniform path model

## Everything As A File Tree

Plan 9 is often summarized as “everything is a file”, but the more useful formulation is:

- many different services should present a file-like tree interface

The point is not to force literal files everywhere. The point is to give users one navigable mental model instead of many unrelated command grammars.

For `ocish`, that means:

- a region is a navigable node
- a domain is a navigable node
- a collection is a navigable node
- a resource is a navigable node

Each path component should mean something stable in the namespace.

## Namespace As The Main Interface

Plan 9 treats the namespace as a first-class interface, not as a passive naming scheme.

That means:

- the structure of paths matters
- mounting matters
- choosing where something appears in the tree matters
- interface design happens through namespace shape, not just command syntax

For `ocish`, this is the most important idea.

The shell should ask:

- where should this OCI object live in the tree?
- what is its parent?
- is it a container, a collection, a resource, or a synthetic control node?
- what path would a user naturally guess?

If the path shape is wrong, the command surface will feel wrong too.

## Per-Process / Custom Namespace Mindset

One of Plan 9’s strongest ideas is that processes can have their own customized namespaces.

The lesson for `ocish` is not that it must literally implement per-process mounts in the Plan 9 kernel sense.

The useful lesson is:

- the visible tree is an interface decision
- different views can be mounted intentionally
- synthetic directories are acceptable when they improve the model

That is why domains such as `core`, `dns`, `logging`, and `load-balancer` fit
after the compartment path. A compartment's default listing exposes immediate
navigation targets: child compartments and present, directly navigable
collections such as `core.vcns`. Domains remain explicit path components.

They are not fake hacks. They are namespace design.

## Small Verb Set

Plan 9 favors small, reusable primitives instead of many bespoke task verbs.

For `ocish`, this means:

- do not add a new top-level command for every OCI action
- prefer reusing path-based verbs where possible
- if a new verb appears, it should be general, not resource-specific

Good examples:

- `ls`
- `cd`
- `cat`
- `find`
- `rm`

Risky examples:

- `delete-vcn`
- `show-nsg-rules`
- `switch-region`
- `wizard-create-oke`

The point is not dogma. The point is to avoid growing a second interface model beside the namespace.

## Synthetic Nodes Are Legitimate

Plan 9 file servers often expose synthetic files and directories that do not correspond to stored objects.

For `ocish`, synthetic nodes are valid when they make the OCI model clearer.

Examples:

- `core/instances`
- `logging/log-groups`
- `load-balancer/load-balancers`
- child collections such as `route-rules` and `security-list-rules`

The rule is not “avoid synthetic things”.
The rule is:

- synthetic nodes should behave like real nodes once exposed

That means they should be:

- reachable by path
- listable if they are directories/collections
- readable with `cat`
- consistent with the rest of the tree

## Canonical Objects and Relationship Projections

OCI is not a pure ownership tree. `ocish` gives every resource one canonical
location based on OCI scope or required parent, then may mount relationship
projections elsewhere. A projection is the same OCID, never a copy.

For example, a Security List is canonical under its compartment while a VCN
can expose it as a related entry. Logging is canonical under
`logging/log-groups/<group>/logs/<log>`, while any related resource can expose
the same object in its lazy `logs/` directory. `canonical-path` makes the
target inspectable.

## Filesystem Model Review (2026-09-18)

The core locator remains the single filesystem transition:

```text
compartment → namespace/type collection → resource → fields / child collections
```

`cd` changes that locator; `ls`/`ll` enumerate its current node; `cat` reads a
node or field; and `pwd` prints its OCI-qualified location.  Child collection
names should be context-relative (`security-rules`, not
`network-security-group-security-rules`) while registry-qualified resource
types remain internal implementation details.

Every exposed node needs a declared capability, rather than being treated as a
generic OCI resource:

- navigable resource: own OCI identity and direct getter; may expose fields,
  child collections, relationships, and logs
- terminal record set: parent-owned inline records; listable and readable, but
  record entries are not directories
- field: terminal value
- relationship: symlink/projection to another canonical resource
- mount/taxonomy node: virtual namespace control or service grouping

This keeps route rules, security rules, DNS-style records, and similar
parent-owned entries from acquiring meaningless `logs` or child directories.

Known intentional exceptions to a literal Unix filesystem model:

- `pwd` emits an OCI locator (`oci:/path@region`), retaining region identity.
- `cat .` prints structured metadata for collections and virtual nodes.
- relationship reads expose a symlink descriptor rather than following it.
- namespace nodes are taxonomy views; compartment nodes list only discovered,
  directly reachable collections.
- `find` is an OCI Search/list operation that synthesizes canonical paths.
  `find .` currently returns raw OCIDs and should eventually offer canonical
  paths by default, with raw IDs as an explicit option.
- `ls -a` is accepted for command compatibility but currently has no distinct
  hidden-node semantics.

## Readability Before Mutation

Plan 9-style interfaces are strongest when the thing you can act on is first something you can inspect.

For `ocish`, that means:

- if a node exists, `cat` should usually tell you what it is
- `ls` should show what children exist
- path discovery should come before mutation

That is why:

- recipe nodes are a better fit than interactive wizards
- delete preview is a better fit than blind mutation
- collection nodes should be readable as nodes, not just listable as containers

## Direct Paths Over Hidden State

Plan 9 tends to reward direct addressing.

The user should be able to say:

- this exact path
- this exact object
- this exact mounted view

and not depend too much on hidden shell mode.

For `ocish`, this means:

- absolute and relative paths should both work cleanly
- including the tenancy root in an absolute path should round-trip
- path semantics should not change unpredictably across node kinds

When the shell surprises the user with “this path shape works here but not there”, the namespace design is leaking.

## Orthogonality

Orthogonality matters more here than feature count.

In a good path-oriented system:

- similar path kinds behave similarly
- the same verbs work across many resource types
- exceptions are rare and clearly motivated

For `ocish`, orthogonality means:

- `cat <path>` should work on all resolved nodes
- duplicate names need stable synthetic disambiguation

## Future Adapter Classes

Most OCI services belong in the ordinary declarative resource model: safe,
compartment-scoped `list_*` collections, resources, declared children, and
relationships.  A service-specific implementation is justified only when its
API shape cannot honestly be represented that way.

There are three reusable adapter classes to develop, rather than a growing
list of service exceptions:

1. **Bounded time/query data.**  Logging is the first instance; Audit should
   be next.  Monitoring, APM, and Log Analytics can later use the same class.
   These expose time-bounded, paged result records, not durable resources.
2. **Hierarchical content.**  Object Storage is the first instance: namespace
   → bucket → object metadata/content.  Content reads need an explicit size,
   binary, and streaming policy; `cat` must not blindly print arbitrary object
   bodies.
3. **Stateful or executable data-plane operations.**  Queue receives change
   message visibility, Streaming reads require consumer cursors, and Function
   invocation executes a workload.  They must be explicit, guarded actions,
   never ordinary `ls` or `cat` browsing.

Audit therefore is not a fourth special case.  It should reuse a generic
bounded-time-query capability while retaining its own canonical namespace,
for example `audit/events/...`, instead of being placed under Logging.
- path segments must be safe and deterministic
- collections, resources, and synthetic nodes must each have a clear but consistent contract

Perfect orthogonality is hard because OCI is not a pure tree. But it should still be the design goal.

## Names Must Be Path-Safe

A file-tree interface only works if entries can actually be addressed.

OCI objects often violate naive path assumptions:

- duplicate display names
- names with slashes or special characters
- anonymous rule-like children

So `ocish` must synthesize stable names when needed.

That is not anti-Plan-9.
That is required to preserve the namespace model.

The important properties are:

- deterministic
- unambiguous
- readable enough
- stable for a given object

## Collections Versus Resources

One major design question is always:

- is this path a collection node?
- or is it a resource node?

The distinction matters.

Good filesystem-style behavior is:

- collection path names the collection node
- resource path names one resource node
- child paths underneath a collection name specific members

When possible, collection members should have stable child names so:

- `ls .../route-rules`
- `cat .../route-rules/<rule>`

works the same way as:

- `ls .../internet-gateways`
- `cat .../internet-gateways/<gateway>`

## Embedded Children Need Real Path Forms

OCI has many cases where “children” are embedded in a parent payload rather than managed as fully separate top-level resources.

Examples:

- route rules
- security-list rules

A Plan 9-ish interface should still expose them as proper child entries if users need to traverse them.

That means:

- do not stop at “the JSON contains a list”
- if users reason about them as children, mount them as children

## Mutation Should Respect Node Kind

Path-oriented mutation can fit the model, but only when the node kind is clear.

Examples:

- `rm <resource-path>` can make sense
- `rm <collection-path>` should not delete the collection
- `rm <domain/type-path>` should not delete anything

Destructive actions should still preserve the namespace contract.

## Preview Before Dangerous Actions

Plan 9 simplicity is not the same as recklessness.

In `ocish`, preview-before-apply is a strong fit for destructive or compound actions.

That is especially true because OCI has:

- async deletes
- dependencies
- service-managed side effects
- cross-compartment references

So:

- preview is consistent with inspectability
- explicit apply is consistent with path-oriented intent

## Graph Reality Under Tree UX

OCI is not a pure tree.

VCNs especially reveal this:

- resources can depend on each other across services
- some consumers live outside the networking service
- some dependencies cross compartment boundaries

So the tree is a user interface, not a proof that the underlying system is tree-shaped.

The design consequence is:

- keep the tree UX
- do not lie about graph complexity
- use planners for recursive teardown
- avoid pretending a container resource is just a simple file

## Recipes Over Wizards

Console-style wizards are not very Plan 9-ish.

They are:

- multi-step
- stateful
- default-heavy
- opaque

A better fit is:

- expose recipes as readable nodes
- show required inputs and defaults as data
- make apply explicit

So instead of a shell wizard, prefer something like:

- `core/recipes/vcn/basic`
- `containerengine/recipes/oke/basic`

with readable preview before creation.

## Honest Interfaces

A Plan 9-ish system should be simple, but not falsely simple.

Bad simplicity:

- hiding ambiguity
- silently picking one of multiple same-named resources
- pretending recursive cloud teardown is equivalent to `rm -r /tmp/x`

Good simplicity:

- clear paths
- small verbs
- explicit previews
- honest error messages
- revealing the real dependency edges when they matter

## Working Rules For `ocish`

If a new feature is proposed, ask:

1. Can this be expressed as a node in the namespace?
2. Can existing verbs handle it?
3. If a new verb is needed, is it generic?
4. Is the node readable before it is mutable?
5. Are names/path segments deterministic and safe?
6. Does the interface stay honest about OCI graph complexity?
7. Does this preserve orthogonality across services?

If most answers are no, it is probably drifting away from the design philosophy.

## Short Version

The Plan 9 ideas most relevant to `ocish` are:

- the namespace is the interface
- services should look like navigable trees
- synthetic nodes are fine when they are principled
- the verb set should stay small
- inspectability comes before mutation
- orthogonality matters
- hidden wizard state is a poor fit
- cloud graph complexity must be exposed honestly when needed

That is the philosophy `ocish` should keep returning to.
