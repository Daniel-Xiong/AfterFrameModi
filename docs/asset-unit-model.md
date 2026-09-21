# Asset unit model

Canonical grouping for the catalog. Physical files stay one-row-per-file;
everything users browse is a stack of groups on top of those rows.

## Layers

```
file row (assets)                  one path, one record
    → (1) capture unit             one shutter: RAW + JPEG + sidecar
        → (2) version family       every unit has one; crops / exports / AI
    → (3) burst group              consecutive capture units; members are (2)
(4) collections / people / geo     tags and filters; not part of the tree
similar-image delete               ephemeral overlay on (2), never a parent
```

### 0. File

`assets` + `asset_files`. Types: `image`, `raw`, `video`. Sidecar `.xmp` files
are recorded on the capture unit (`capture_unit_sidecars`) and are not
browseable tiles.

### 1. Capture unit

Same shutter. Typical members:

- `display` — JPEG / HEIC / PNG (or RAW when that is the only file)
- `raw` — the paired RAW
- in-camera JPEG sits with the RAW as the same unit, not a second tile

Identity is catalog-owned (`cu_…`). RAW matching (`image_lookup_registry`) and
same-stem siblings both feed this layer. Reverse-lookup source RAWs that never
appear in the gallery still attach as `raw` members.

Every capture unit **must** own a resource set (layer 2), even if it has only
the main version.

### 2. Version family (`resource_sets`)

Crops, compressed exports, editor saves, AI repaints. `parent_asset_id` +
`version_kind`. The gallery representative is `capture_units.display_asset_id`
(usually the set primary). Derived versions are not their own capture units
and are hidden from the default gallery.

### 3. Burst group

Same camera, capture times within `BURST_GAP_SECONDS` (default 2s), size ≥ 2.
Items reference **layer-2 handles** (`resource_set_id` + `unit_id`), never raw
file rows. Default cover is the highest-rated member, else earliest shot.
Burst UI is pick-keepers / delete rejects; it does not invent new photos.

### 4. Overlay dimensions

Collections, faces, and locations hang off an asset (usually the display
version). They do not nest inside (1)–(3).

## Similar-image delete

Runs on **layer-2 representatives** only (capture-unit display assets).
Clusters are query results (`list_similar_clusters`), not schema parents.
Pairs already in the same burst are omitted — bursts already cover that case.

## Browse rule

Default gallery (`representatives=true`):

- one card per capture unit that is not in a burst
- one card per burst (the keeper)
- hide version siblings and merged RAW tiles

Collections list exact membership (a crop added to an album stays visible).

## Rebuild

`rebuild_capture_graph` is the single writer. It runs after import, after
derived registration, and on schema v9 migrate. Idempotent: deletes the graph
tables and rebuilds from `assets` / `resource_sets` / `image_lookup_registry`.
