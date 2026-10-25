# Street-level imagery for building facades

Research date: 2026-10-24

## Question

Can BeamNG.Earth use street-level imagery to make its generated buildings resemble the actual buildings along the road?

## What the current pipeline has

Buildings are made in `server/buildings.py` from Overture footprints, height/floor data, and optional Overture building parts. The renderer predicts missing roofs and facade types and writes the result into a tile `.cdae`. The road tile contains a colliding building mesh; distant building tiles use lighter render-only geometry. Overture building parts can provide shape and some roof/facade attributes, but they are map geometry rather than textured 3D assets. See the [Overture building guide](https://docs.overturemaps.org/guides/buildings/) and [building part schema](https://docs.overturemaps.org/schema/reference/buildings/building_part/).

The first branch change makes mapped Overture facade and roof colors affect the building mesh, including colors supplied per building part. This gives the current provider's explicit appearance data priority over a guessed tint and creates a small, reviewable step toward photo-derived facade attributes.

BeamNG.Earth's existing street imagery references in the building notes are developer visual comparisons. The runtime data-source list currently contains aerial/satellite imagery and map vectors, not a street-view service. Overhead imagery can help identify roof colors and footprints; it cannot supply a street-facing facade texture.

## Providers

### Google Street View

Do not build a model or texture extraction path from Google Street View. The current [Google Maps Platform terms](https://cloud.google.com/maps-platform/terms) prohibit creating content from Maps content, explicitly including 3D building models from 45-degree imagery, and prohibit using Maps content to train, test, validate, or fine-tune machine-learning models. The terms also restrict caching Maps content outside the service. A street-photo-to-facade pipeline would depend on prohibited derivative use.

### Mapillary

Mapillary is the strongest street-level candidate found in this review. Mapillary says its imagery is shared under CC BY-SA and its API exposes images and image metadata. Its developer terms require a registered application and visible Mapillary attribution; the terms also say the app must materially supplement Mapillary rather than merely redistribute or replicate its service. Mapillary's upload pipeline automatically blurs faces and license plates, though it documents that users can request additional blurring.

Before shipping model or texture derivatives, confirm in writing that the intended automated facade reconstruction is allowed under the current API terms and image license. In particular, confirm whether generated mesh/texture assets are adaptations that must be distributed under CC BY-SA, how image attribution should appear in the in-game credits, and what limits apply to image caching and processing. The image license alone should not be treated as an answer to API-specific terms.

References:

- [Mapillary CC BY-SA explanation](https://help.mapillary.com/hc/en-us/articles/115001770409-CC-BY-SA-license-for-open-data)
- [Mapillary Terms of Use](https://www.mapillary.com/terms)
- [Mapillary privacy information](https://help.mapillary.com/hc/en-us/articles/115001770349-Privacy)

## Technical approach

Street images are best used to add authentic facade appearance to the existing map-derived shape. A single view cannot reveal the whole building: it may hide upper floors, sides, rear walls, roof geometry, and ground-level details behind vehicles or trees. Full 3D reconstruction from sparse crowdsourced images would have uneven quality and high processing cost.

Recommended first implementation:

1. Query eligible street-image metadata near a building using its Overture footprint and the road graph. Keep provider image IDs, capture dates, camera positions, compass headings, and attribution with each observation.
2. Select images whose camera faces the footprint, whose viewing angle is near the facade normal, and whose projected facade is large and unobstructed. Require a confidence threshold and more than one view where possible.
3. Segment the visible facade, rectify its quadrilateral to a canonical wall texture, and preserve the image's privacy blur. Avoid keeping raw images longer than the provider terms allow.
4. Keep Overture/OSM geometry for collision and shape. Apply the rectified photo to only the corresponding near-facing wall; use the current procedural materials for other walls and for all distant LODs.
5. Store derived results in a versioned, provider-aware cache keyed by building source ID, image IDs, and processing version. Keep provider/license provenance so the in-game attribution can include sources for the visible assets.

This uses the current building mesh path but needs a new per-building facade-texture atlas. The existing renderer batches buildings into tile `.cdae` files with a fixed material set; photo textures therefore need packing into tile or cache-cell atlases with UV rectangles, rather than adding one permanent game material per building. Nearby collisions should stay on the simplified geometry, with textured facades render-only if the provider mesh creates additional vertices or polygons.

## Phased work

### Phase 0: coverage and rights gate

- Check Mapillary image coverage on a small representative route.
- Confirm API/application terms and the share-alike consequences for cached derived assets.
- Define cache lifetime, attribution presentation, image deletion handling, and a no-image fallback.

### Phase 1: a single-street prototype

- Add a Mapillary metadata client that requests only camera metadata in a bounded area and respects rate limits.
- Manually review a sample of selected images and facade crops before automating extraction.
- Build a facade atlas for one road tile and display it on the near building mesh.
- Compare against screenshots and profile image fetch, processing, and import costs.

### Phase 2: automated enrichment

- Add facade segmentation, visibility/occlusion scoring, rectification, and deterministic selection among captures.
- Make image work asynchronous and lower-priority than terrain/road builds.
- Expose coverage and confidence in diagnostics; fallback to current procedural buildings whenever coverage is poor.

### Phase 3: broader rollout

- Expand only after the pilot meets image-quality, performance, attribution, license, and cache-deletion requirements.
- Keep distant buildings on procedural low-detail geometry so model and texture cost stays bounded.

## Decision

Street-level imagery can materially improve the facade on the side facing a road, but it does not provide complete building models on its own. Mapillary is a plausible pilot source pending written terms confirmation. Google Street View is excluded from derivative model generation under its published Maps Platform terms. The first deliverable should be a one-street Mapillary facade-texture prototype, not global photogrammetric reconstruction.
