# ImageAsset Model

The catalog should stop thinking in terms of file lists and start thinking in terms of one abstract image object with attached physical resources.

## Core Object

`ImageAsset`

- One logical image identity
- One canonical source RAW
- Zero or more export files
- Zero or more derived files
- Zero or more cached previews and proxies

## Physical Resource Set

An `ImageAsset` can own or reference these resource kinds:

- `raw_source`
- `export_variant`
- `derived_variant`
- `preview_cache`
- `proxy_cache`

For the current product scope:

- `raw_source` should exist at most once
- Multi-source composites are out of scope for now

## Why This Matters

This abstraction lets the system answer the product questions directly:

- "What is the source RAW for this export?"
- "What previews exist for this image?"
- "Which publish artifacts came from this photo?"
- "Which derived covers and captions belong to this image?"

Without this object, the app keeps falling back to ad hoc joins between unrelated files.

## Lookup Implication

Reverse lookup is not really:

- export file -> raw file

It is:

- export file -> `ImageAsset`
- `ImageAsset` -> canonical `raw_source`

That distinction matters because registry, previews, content drafts, and publish state should all attach to the abstract asset, not to whichever file happened to be matched first.

## Suggested Identity Rules

- `image_asset_id` is stable and catalog-owned
- `raw_asset_id` stays file-derived and path-relinkable
- Exports and derived files link into the `image_asset_id`
- Registry should persist:
  - export file
  - matched `image_asset_id`
  - matched `raw_asset_id`
  - match rationale
  - confirmation state

## Matcher Structure

Matcher should be three-stage:

1. Candidate recall
2. Hard veto rules
3. Final decision

Examples of hard veto rules:

- camera model conflict
- impossible timestamp delta
- incompatible aspect/dimension constraints
- filename family conflict

Examples of soft signals:

- stem similarity
- weak EXIF overlap
- preview hash similarity
- directory or shoot proximity

## Performance Implication

This abstraction is also the right performance boundary:

- scanning populates physical resources
- matching resolves `ImageAsset`
- UI reads a compact aggregate row per logical image
- preview generation operates on cached resources, not arbitrary files

That is the path toward a catalog that behaves more like Lightroom than a generic DAM clone.

## Visual Relationship Cleanup

Exact files, compressed exports, and crops can be proposed as members of one
`ImageAsset`. Bursts and merely similar images remain separate identities, and
RAW-source proposals continue through the existing registry confirmation path.

The bounded recall, confirmation, root-scope, and file-safety contracts are
defined in [Duplicate, Similar, Crop, and Source Matching](duplicates-similar-design.md).
