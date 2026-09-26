# Recipes vs Wizards

Date: 2026-09-02

OCI Console wizards can quickly create full VCNs, OKE clusters, and other compound environments. That outcome is useful, but the wizard interaction model is not a strong fit for `ocish`.

## Why Console Wizards Do Not Fit Well

Console wizards are:

- task-oriented
- multi-step and stateful
- full of hidden defaults
- compound in their side effects
- optimized for guided creation, not inspectable composition

That is almost the opposite of the `ocish` direction:

- small verbs
- path-addressable objects
- explicit composition
- inspectable intermediate state
- limited hidden behavior

## What Fits Better

The better `ocish` analogue is a recipe model instead of a wizard model.

Examples:

- `core/recipes/vcn/basic`
- `containerengine/recipes/oke/basic`

Those recipe nodes should be readable first.

`cat` on a recipe should show:

- the resources that would be created
- the defaults that would be applied
- required inputs
- optional inputs
- important side effects

Creation should then happen through an explicit apply-style step rather than an opaque interactive wizard.

## Plan 9-ish Version

The Plan 9-ish implementation is:

- recipes are exposed as nodes
- defaults are visible as data
- the intended graph is readable before creation
- apply is a separate explicit act

The non-Plan-9-ish version would be:

- a large interactive shell wizard
- hidden multi-step state
- one command that silently creates many resources with little inspection

## Guiding Principle

If `ocish` ever supports “quick create” for things like VCNs or OKE clusters, it should prefer:

- recipe nodes
- previewable plans
- explicit apply

and avoid:

- opaque shell wizards
- hidden defaults
- surprise side effects
