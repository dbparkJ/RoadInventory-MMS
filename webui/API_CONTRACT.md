# MMS Studio Web API contract

The frontend uses same-origin `/api` requests by default. Set `VITE_API_BASE_URL` only when the
browser should call a separate API origin. During local development, `MMS_API_TARGET` controls the
Vite proxy target and defaults to `http://127.0.0.1:8000`.

All JSON uses UTF-8 and snake_case. Errors should return a non-2xx status with one of:

```json
{ "message": "operator-facing message", "code": "STABLE_CODE" }
```

or FastAPI's compatible `{ "detail": "message" }`. GET/HEAD requests are retried twice on network
or 5xx failures. Mutating requests are not automatically retried.

## Bootstrap and storage

`GET /api/bootstrap`

```json
{
  "api_version": "1",
  "server_name": "MMS Processing Server",
  "map": {
    "provider": "vworld",
    "engine": "webgl",
    "version": "3.0"
  },
  "capabilities": {
    "upload": true,
    "panorama": true,
    "point_cloud": true,
    "auto_optimize": true
  },
  "datasets": ["DatasetSummary"],
  "recent_runs": ["RunRecord"]
}
```

`map` identifies the fixed browser map integration. The API does not accept or publish an external
map style URL. The non-satellite 2D view creates one OpenLayers tile layer from VWorld's official
`Base` WMTS endpoint, with no OSM fallback layer. The WebGL 3.0 map is loaded by an isolated
same-origin iframe. Their browser-visible development key and current origin are sent directly to
VWorld rather than through bootstrap.

An unavailable bootstrap activates a read-only browser demo. A successful response with no
datasets displays the data-source empty state and opens the connection dialog.

`GET /api/storage` returns `{ "roots": [{ "id", "name", "path_hint?", "writable",
"free_bytes?" }] }`.

`GET /api/storage/{root_id}/tree?path={relative_path}` returns:

```json
{
  "root_id": "capture",
  "relative_path": "2026/segment-a",
  "entries": [
    {
      "name": "run-001",
      "relative_path": "2026/segment-a/run-001",
      "type": "directory",
      "modified_at": "2026-07-31T05:40:00Z",
      "dataset_hint": true
    }
  ]
}
```

Only server-issued root IDs and relative paths are sent back; the browser never receives or
constructs an absolute server path.

## Dataset catalog

`POST /api/datasets/scan`

```json
{
  "root_id": "capture",
  "relative_path": "2026/segment-a/run-001",
  "crs": "EPSG:32652"
}
```

`crs` may be blank for server detection. The UI explicitly offers EPSG:32652, EPSG:5179, and
EPSG:5186 because the LAS and pose CRS must match. The response is a `DatasetDetail`. It may first
have `status: "indexing"`; the client polls `GET /api/datasets/{id}` every 1.5 seconds for up to two
minutes and accepts the dataset only when it becomes `ready`.

`DatasetSummary`:

```json
{
  "id": "ds_01",
  "name": "segment-a",
  "relative_path": "2026/segment-a/run-001",
  "status": "ready",
  "frame_count": 1200,
  "point_count": 86420000,
  "distance_m": 4820,
  "size_bytes": 18620000000,
  "crs": "EPSG:32652",
  "captured_at": "2026-07-29T00:12:00Z",
  "tracks": [
    { "id": "track-a", "name": "구간 A", "frame_count": 720, "distance_m": 2940 }
  ]
}
```

`DatasetDetail` adds optional `bounds`, `sensors`, and `indexed_at`.

`DELETE /api/datasets/{id}` unregisters a dataset from the workspace and returns
`{ "id", "removed": true, "source_deleted": false, "detail" }`. It removes derived frame rows but
never deletes the source folder. A queued, preparing, starting, running, or cancelling run returns
`409`; terminal run history remains available.

`GET /api/datasets/{id}/route` returns `{ "points": RoutePoint[] }`, where each point includes
`lon`, `lat`, optional `altitude`, `frame_id`, `track_id`, and `heading`. The client builds a
separate LineString for each `track_id`, avoiding false connector lines between tracks.

`GET /api/datasets/{id}/frames?offset=0&limit=240&track=track-a` returns:

```json
{
  "items": [
    {
      "id": "frame-0001",
      "index": 0,
      "track_id": "track-a",
      "timestamp": "2026-07-29T00:12:00.000Z",
      "coordinate": { "lon": 126.978, "lat": 37.566, "altitude": 31.2 },
      "heading": 38,
      "speed_kph": 32,
      "has_panorama": true,
      "has_points": true
    }
  ],
  "offset": 0,
  "limit": 240,
  "total": 1200,
  "next_offset": 240
}
```

`coordinate` may be `null` when pose CRS transformation failed. Such a frame stays usable in the
panorama/frame list and is omitted only from map markers.

`GET /api/datasets/{id}/frames/{frame_id}/address` resolves the frame's WGS84 pose to a current
address for panorama context. A delivery-supplied `road_address`/`address` is preferred. Otherwise
the server proxies V-World Geocoder API 2.0 because that API does not expose browser CORS headers;
it tries `ROAD`, then `PARCEL`, with a short timeout, a two-request concurrency limit, and a bounded
32-coordinate in-flight backlog; excess demand falls back to coordinates immediately. The
browser waits 300 ms after a frame change and aborts stale requests. V-World's usage terms prohibit
storing real-time geocoder responses in a separate database, so successful results are not written
to SQLite or disk; only concurrent identical requests are coalesced in memory. Failure responses
use a 30-second in-memory suppression window and return `address: null`, leaving coordinates as the
UI fallback.
The governing service reference is the official [V-World Geocoder API 2.0
GetAddress guide](https://www.vworld.kr/dev/v4dv_geocoderguide2_s002.do).

```json
{
  "dataset_id": "ds_01",
  "frame_id": "frame-0001",
  "coordinate": { "lon": 126.978, "lat": 37.566 },
  "address": "서울특별시 중구 세종대로 110",
  "address_type": "road",
  "zipcode": "04524",
  "source": "vworld"
}
```

## Preview media and performance

`GET /api/datasets/{id}/panoramas/{frame_id}?width={width}` returns an image body, or JSON
`{ "url": "signed-or-cache-url" }`. The client requests this only when the panorama tab is opened,
using a viewport-derived fast preview up to 2048px, a cache-stable 4096px high-quality preview, or
an explicitly selected 8192px preview. The original panorama is never requested by the operator
workspace.

`GET /api/datasets/{id}/points/{frame_id}?budget=250000` returns
`application/vnd.mmsp` (or `application/octet-stream`). The browser never requests raw LAS.
`budget` defaults to 250k and is operator-selectable (250k/500k/1m); values below 250k or above 1m are
rejected. At the maximum, the compact MMSP body is bounded to 15,000,040 bytes. Per-dataset MMSP
derivatives are capped at 1 GiB and evict the oldest files first. The point preview has a server-owned spatial
contract: only samples within 25 m of the acquisition origin are eligible; by default 75% of the
available budget goes to the dense 0–15 m band and 25% to the lower-density 15–25 m band. Unused
capacity in either band is deterministically reassigned to the other, while per-block quotas retain
nearby block coverage. There is no client radius override. MMSP cache fingerprint `mmsp-v3`
includes both band limits and the allocation ratio so older wide-radius derivatives are not reused.

MMSP v1 is little-endian:

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | ASCII 4 | `MMSP` |
| 4 | uint16 | version = 1 |
| 6 | uint16 | flags; bit 0 means RGB |
| 8 | uint32 | point count |
| 12 | float32 × 3 | local bounds min XYZ |
| 24 | float32 × 3 | local bounds max XYZ |
| 36 | uint32 | reserved |
| 40 | records | local XYZ float32 × 3, then optional RGB uint8 × 3 |

The server may return `202 application/json` with `{ "status": "indexing" }` and `Retry-After`
while the LAS preview catalog is prepared. The point viewer shows an indexing state and retries
with bounded backoff (eight attempts); changing frames or tabs aborts the sequence.

`GET /api/datasets/{id}/panorama-points/{frame_id}?budget=30000&radius=30&cell_size_px=3`
returns `application/vnd.mmso`. Optional `yaw_offset_deg` and `pitch_offset_deg` override the
pipeline alignment defaults published in `bootstrap.preview_defaults`. Omitting them uses the
server's validated panorama alignment configuration. The server projects in the frame's original
pose/point-cloud CRS, keeps the nearest point per screen-space cell on a virtual 4096x2048 sphere,
and caches the frame derivative. The derived MMSO cache is capped at 512 MiB per dataset and evicts
the oldest entries first. This avoids shipping raw world coordinates, limits panorama overdraw, and
prevents an unbounded cache during long review sessions.

MMSO v1 uses the same 40-byte header and 15-byte record stride as MMSP. Its magic is `MMSO`, flags
bit 0 means RGB and bit 1 means normalized equirectangular coordinates, header bounds are minimum
and maximum `(u, v, distance_m)`, and each record is `u:f32, v:f32, distance_m:f32, rgb:u8x3`.
`u` and `v` are normalized to `[0, 1]`.

The VWorld Base WMTS 2D general-map and WebGL 3.0 views use separate same-origin iframes so each
map runtime is isolated from React and cross-document popup moves. The map view, spherical
panorama renderer, and point-cloud renderer are
lazy-loaded; the external VWorld SDK is not bundled by Vite. The shared Three.js dependency is
excluded from the initial application bundle.

`GET /api/datasets/{id}/frames/{frame_id}/panorama-projection` returns the calibrated dataset-space
`origin`, `forward`, `right`, and `up` vectors used by the server's equirectangular projection.
The point viewer fetches this once per frame and projects hovered MMSP local XYZ to normalized
panorama `(u, v)` in the browser; pointer movement never triggers per-point HTTP requests.

## SHP review and editing

`GET /api/datasets/{id}/overlays` lists registered SHP layers. Feature collections are paged by
`GET /api/datasets/{id}/overlays/{layer_id}/features?coordinate_space=dataset|wgs84&offset=...&limit=...`.
The client keeps layer selection, visibility, loaded pages, and feature selection in one shared
context so the attribute editor, map, panorama, and point viewer stay in sync. The main dataset panel
only exposes a searchable layer list and visibility toggles. Layer upload/name/color controls remain
in `SHP management`, while the full attribute table is rendered only in its independent popup.

`PATCH /api/datasets/{id}/overlays/{layer_id}` changes the user-facing `name` and/or shared
`color` (`#RRGGBB`). It requires `expected_metadata_revision`; a stale value returns `409`, while an
empty/unsafe name or invalid color returns `422`. The response contains the updated layer and next
`metadata_revision`. These display settings live in the overlay manifest, survive application
restart, and do not increment the independent feature `revision`.

`POST /api/datasets/{id}/overlays/{layer_id}/features` creates a feature with exactly one of:

- `geometry` plus `coordinate_space` for a clicked Point position; or
- `copy_geometry_from` for a geometry-only copy of an existing feature in the same layer.

Both forms accept `expected_revision` for optimistic concurrency. Map-click creation is restricted
to Point layers. A copied feature keeps no user attributes: every DBF field is blank except an exact
case-insensitive `ID` field, which is assigned the next numeric value. The durable internal feature
ID and ordinal also increase monotonically and are not reused after deletion. The response is the
new feature and updated revision; subsequent edited-bundle ZIP exports include the new row.

`PATCH /api/datasets/{id}/overlays/{layer_id}/features/{feature_id}` updates coordinates and/or
properties, and `DELETE` marks a feature deleted from the editable copy. The original uploaded SHP
bundle is preserved. Revision conflicts return `409`; invalid geometry, field values, or coordinate
space return `422`.

`DELETE /api/datasets/{id}/overlays/{layer_id}/fields/{field_name}?expected_revision=...` removes a
DBF column from every feature in the editable store, advances the feature revision once, records a
`delete_field` audit entry, and removes the field from subsequent edited SHP exports. It never mutates
the uploaded source bundle. Exact case-insensitive `ID`, required/internal fields, and the final
remaining DBF field are protected; stale revisions return `409`.

`GET /api/datasets/{id}/overlays/{layer_id}/project/{frame_id}` returns nearby Point features as
normalized panorama coordinates and includes their complete properties. The optional
`max_distance` bounds the query.

`GET /api/datasets/{id}/frames/{frame_id}/detections` independently returns the bounded raw YOLO
observations for every model in the newest completed run that contains an exact record/image result
for that frame. It does not require a result SHP to be imported or visible, and dismissed queue rows
remain readable because their durable run artifacts are preserved. The server resolves only exact
server-managed `output/{model}/txt/{record}/{image}.txt` files, rejects traversal, links, oversized
files, mismatched payload identities, and unknown bbox spaces. A request parses at most 64 MiB of
model manifests plus result JSON across the completed-run lookup, at most 64 models, and returns at
most 2,000 boxes. Reaching any of those limits stops scanning immediately and sets `truncated: true`.
Each observation has an opaque per-run/model `source_id` for deterministic deduplication.
The response also includes `models: [{model_id, source_id, source_name, count}]` for every parsed model,
including models with zero observations in the selected frame, so the panorama can keep independent
visibility controls stable while the operator moves between frames. New clients derive the list
from observations when an older server omits this field; older clients may safely ignore it.
`model_id` is an opaque stable identity derived from the server-owned model key, so two models with
the same display filename remain independently controllable. Observations carry the same optional
`model_id`; legacy responses fall back to their per-run/model `source_id`.
An observation also has `dataset_position: [x, y, z]` only when the pipeline accepted a finite
point-cloud representative for SHP output. Candidate/rejected coordinates are never exposed as a
3-D detection. The point viewer converts accepted dataset coordinates to the frame-local MMSP
space, limits them to the same 25 m preview, and renders an identity wireframe plus center marker;
the wireframe is not presented as an inferred physical object extent. Matching visible SHP
representatives preserve layer/feature Details only when detection ID and compatible model, image,
and class metadata agree, while the marker itself does not depend on SHP import or visibility. All
bounded observations returned by the endpoint are considered before the 25 m / accepted-position
filter; there is no earlier client-side 512-item cutoff.

Pipeline result schema 18 explicitly stores each `bbox_xyxy` as
`panorama_equirectangular_pixels` after the full, tiled, or forward perspective detector has been
inverse-mapped to its source panorama. Schema 17 has the same established coordinate contract and
is supported for existing results. An explicitly labelled legacy `forward_rectilinear_pixels` box
is inverse-mapped only when complete forward-view dimensions, FOV, panorama dimensions, and yaw /
pitch alignment metadata are present; the server never guesses an unlabeled legacy coordinate
space. The panorama client requests this endpoint once per frame and renders the boxes regardless
of SHP layer visibility. A completed-run revision change re-fetches the same selected frame, so an
SSE completion exposes newly written boxes without requiring navigation or reload. When a raw box
matches a representative feature in a visible result SHP by detection identity, compatible model /
image / class metadata, and panorama bbox, the client draws only the raw box but retains the SHP
`layerId` / `featureId` for selection and Details; unrelated raw observations remain unlinked.
Panorama boxes use a repeat-wrapped 4096x2048 overlay texture for seam-safe rendering, a thin
1.5 px line (3 px when selected), with no always-visible text tag. Hovering a box opens the compact
overlay tooltip with the class as its title and `conf` among the prioritized values. A linked SHP
representative shows its user-defined layer name/color in the header; an unlinked raw observation
shows its model name/stable model color. Clicking pins the same tooltip. This prevents neighboring
objects from producing overlapping labels. Per-model panorama switches filter rendered boxes,
hit-testing, and the visible YOLO count together; the stable-model-ID choice persists across frame
changes.

## Optimization and runs

`POST /api/optimize` accepts the same `RunRequest` as creating a run and returns:

```json
{
  "parameters": {
    "voxel_size": 0.1,
    "confidence": 0.8,
    "cluster_distance": 0.35,
    "min_points": 100,
    "search_radius": 15,
    "ground_tolerance": 0.35
  }
}
```

`POST /api/runs` accepts:

```json
{
  "dataset_id": "ds_01",
  "track_ids": ["track-a"],
  "frame_range": null,
  "mode": "automatic",
  "auto": { "preset": "balanced" }
}
```

Automatic mode selects a processing-resource profile for the server hardware and operator preset; it
does not train a model, sample frames, or silently change the validated algorithm thresholds.
For manual mode, omit `auto` and provide the `parameters` object above. The validated UI defaults
match the server configuration rather than inventing dataset-independent values.

`GET /api/runs` returns `{ "items": RunRecord[] }`. `POST /api/runs/{id}/cancel` returns the updated
record. Progress is 0–100 and status is one of `queued`, `preparing`, `running`, `completed`,
`failed`, `cancelled`, or `cancelling`.

`GET /api/datasets/{dataset_id}/runs/latest-completed` returns
`{ "run": RunRecord | null }`. It queries the durable run registry for that dataset rather than
the bounded recent queue, and therefore includes a completed run dismissed from queue/bootstrap
visibility. "Latest" is ordered by actual `finished_at`, with `updated_at` then `created_at` as
legacy fallbacks; `created_at` and run ID provide deterministic tie-breaks. The endpoint returns
`404` for an unknown or unregistered dataset. It remains the compact compatibility endpoint for
clients that need only one result; `null` means that the selected dataset has no completed run.
During rolling upgrades, a web client receiving `404` for the completed-history endpoint falls back to
`GET /api/runs?limit=200` and selects the newest completed run for the active dataset. That legacy
fallback cannot recover a run already dismissed by an older server, so durable dismissed-result
lookup becomes complete as soon as the new endpoint is deployed or the API process is restarted.

`GET /api/datasets/{dataset_id}/runs/completed?limit=200&offset=0` returns every durable completed
job as a paged history, including queue-dismissed rows. The response is
`{ "items": RunRecord[], "offset": 0, "limit": 200, "total": 241, "next_offset": 200,
"snapshot_at": "2026-08-14T01:02:03+00:00" }`; `next_offset` is `null` on the last page. The web
client sends the first response's `snapshot_at` on every later page, so a run completing while the
history is loading cannot shift OFFSET boundaries and duplicate or hide rows. It follows every
stable page before showing the job list, and selecting a row opens that exact run's result detail
rather than silently choosing only the latest run.

`frame_range` always contains zero-based **dataset-global ordinals**, even when `track_ids` contains
one track. The automatic-detection setup therefore validates range inputs against the full dataset
frame count; the server intersects that global range with the requested tracks.

### Field-survey sections

`GET /api/datasets/{dataset_id}/survey-segments` returns
`{ "items": SurveySegment[] }`. A segment has an opaque ID, display name, `#RRGGBB` color and a
WGS84 `LineString` geometry. `POST` to the same path accepts
`{ "name": "현장조사 필요구간 1", "color": "#f59e0b", "coordinates": [[127,37],[127.01,37.01]] }`
and returns `{ "segment": SurveySegment }` with status 201. Between 2 and 5,000 finite WGS84
vertices are accepted. `DELETE /api/datasets/{dataset_id}/survey-segments/{segment_id}` returns
`{ "id": segment_id, "deleted": true }`. Segments are stored in the web registry, cascade with
the dataset record, and render as a separately toggleable line layer in both VWorld 2D and 3D.

`DELETE /api/runs/{id}` dismisses a completed or failed run from collection/bootstrap responses.
This is a visibility operation, not artifact deletion: the response contains
`{ "dismissed": true, "artifacts_preserved": true }`, and direct run, result, and archive URLs
remain valid. Internally recovered `interrupted` rows are eligible because they are projected as
`failed`; queued, active, and cancelled rows return `409`.

`status` is the stable v1 UI lifecycle field and retains the values above. A run backed by the
versioned execution manifest can additionally include the following fields; old clients may ignore
them, and the server may omit them for legacy runs that do not have a manifest:

```json
{
  "id": "run_01",
  "job_id": "run_01",
  "status": "running",
  "canonical_status": "running",
  "attempt": 1,
  "manifest_schema_version": 1,
  "current_stage": "detect_project_and_estimate",
  "error_info": null,
  "versions": {
    "git_commit": "8d3c7f1",
    "config_hash": "sha256:...",
    "config_schema": 1,
    "model_hashes": { "sign": "sha256:..." },
    "calibration_id": "cal-2026-07",
    "calibration_hash": "sha256:..."
  },
  "counts": {
    "images": 420,
    "detections_2d": 37,
    "projected_3d": 31,
    "valid_features": 26,
    "rejected_features": 5
  },
  "stage_results": [
    {
      "attempt": 1,
      "stage_name": "validate_inputs",
      "stage_version": "1",
      "status": "succeeded",
      "started_at": "2026-08-04T01:00:00Z",
      "finished_at": "2026-08-04T01:00:01Z",
      "elapsed_ms": 1000,
      "input_count": 420,
      "output_count": 420,
      "rejected_count": 0,
      "metrics": {},
      "warnings": []
    }
  ]
}
```

`canonical_status` is one of `pending`, `validating`, `running`, `succeeded`, `failed`, `retrying`,
or `cancelled`. `error_info`, when present, contains `code`, `message`, `stage`, `job_id`, and
`retryable`, with optional `object_id`, `context`, and `cause_type`. Manifest-derived paths and
diagnostics exposed by this public projection must be server-relative and operator-safe; absolute
input, model, calibration, config, and output paths are not part of `RunRecord`.

`attempt` starts at 1. A failed manifest can be moved explicitly through
`failed -> retrying -> running`, which increments the attempt and resets progress, counts, and
declared outputs while preserving prior diagnostic history. The current Web API has no automatic
retry policy or retry endpoint; `retrying` is a forward-compatible canonical state, not an implied
automatic action.

`GET /api/runs/{id}/artifacts` parses every downloadable `.json` and pipeline `.txt` JSON artifact,
recursively redacts server paths, and returns the safe JSON as an attachment. This includes the run
and models manifests as well as frame result TXT files. Run/model manifests are limited to 5 MB and
other JSON/TXT artifacts to 25 MB. Empty, non-UTF-8, or oversized artifacts and malformed `.json`
return `404`. A plain-text `.txt` that is not JSON is returned only after bounded UTF-8 decoding and
inline path redaction. Pipeline `.log` files and files below an output `logs/` directory are
diagnostic data and are not exposed through the general artifact-download endpoint. The redacted
`log_tail` on `GET /api/runs/{id}` remains the operator-facing diagnostic view.

`GET /api/runs/{id}/archive?scope=all` returns one ZIP containing every publicly downloadable
result while preserving output-relative paths. `scope=detected-images` returns only images below an
`image_crops` directory; shared `forward_views`, point previews, and pole debug images are not part
of that focused archive. Both scopes reject active or unverified completed runs, skip logs,
unsupported files, symbolic links, junctions, stale undeclared Shapefile bundles, and untrusted
models manifests. JSON/TXT members use the same bounded parsing and recursive server-path redaction
as the individual artifact endpoint. `GET /api/runs/{id}/results` advertises both links under
`archives.all` and `archives.detected_images`; the operator UI downloads these ZIPs instead of
listing every ordinary artifact separately.

A worker exit code of zero is not sufficient for `completed`: the run manifest must be valid and
`succeeded`, and every Shapefile declared by the current attempt must have a complete, non-empty
`.shp/.shx/.dbf/.prj/.cpg/.qpj/.wkt2` bundle below that run's output root. The server does not
rescan the directory to infer successful outputs: result summaries, artifact links, ZIP downloads,
and imports expose only manifest-declared Shapefiles. For a multi-model run, the declared models
manifest must also be valid schema version 2, every model must be a completed current-run
publication, and its Shapefile set must exactly match the root run manifest. A contract-versioned
run whose manifest is
missing or whose output later becomes incomplete is projected as failed and omits `result_url`.
Runs created before the execution-contract marker remain on the legacy bounded-result fallback.

The server records the exact SHA-256 of the generated YAML handoff before queueing. The child must
read the same file hash under the same job/input identity before it can claim the pending manifest;
after defaults and supported overrides are resolved, the canonical effective-config hash is stored
separately as provenance.

On server restart, only a pre-spawn `preparing` row is requeued. A possibly spawned `starting` row
and `running`/`cancelling` rows are reconciled to a terminal DB/manifest state; a complete durable
`succeeded` manifest wins a late cancellation or shutdown race. There is no stage-checkpoint resume.
The manifest input section records the selected image-task count/fingerprint, at most 1,000 relative
path samples, and a truncation flag; those private inputs are available only through the redacted
manifest artifact and are not copied into `RunRecord`.

Clients must treat unrecognized `status` values as forward-compatible data. They should render a
neutral fallback instead of indexing an exhaustive status table without a fallback. The current UI
uses `canonical_status` for a more specific label when available.

`GET /api/runs/{id}/events` is an SSE stream. The preferred format is a named `event: run` whose
data is either a `RunRecord` or `{ "run": RunRecord }`. Default messages and named `progress`,
`stage`, `completed`, `failed`, and `cancelled` events are also accepted for compatibility.
Snapshot events omit `log_tail`; progress and the current stage come from the manifest, with the
legacy bounded log parser used only when no valid manifest exists.

## Resumable folder upload

`POST /api/uploads`

```json
{
  "name": "run-001",
  "files": [
    {
      "path": "run-001/LAS/0001.las",
      "size": 123456789,
      "type": "application/octet-stream",
      "last_modified": 1785451200000
    }
  ]
}
```

The response supplies `{ "id", "chunk_size", "files": [{ "id", "path", "uploaded_bytes?" }] }`.
Before each file, the client issues `HEAD /api/uploads/{session}/files/{file}` and reads
`Upload-Offset` or `X-Uploaded-Bytes` when present. Each chunk is sent with:

```text
PUT /api/uploads/{session}/files/{file}
Content-Type: application/octet-stream
Content-Range: bytes {start}-{end}/{total}
X-Relative-Path: {url-encoded relative path}
```

Up to three files are transferred concurrently. The operator can abort the active requests.
`POST /api/uploads/{session}/complete` returns `{ "root_id", "relative_path", "upload_id" }`; the
client then runs the normal dataset scan/indexing flow.
