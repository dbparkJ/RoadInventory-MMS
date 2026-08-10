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
map style URL. VWorld WebGL 3.0 is loaded by the same-origin map iframe, and its browser-visible SDK key
and current origin are sent directly to the VWorld loader rather than through bootstrap.

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

## Preview media and performance

`GET /api/datasets/{id}/panoramas/{frame_id}?width={width}` returns an image body, or JSON
`{ "url": "signed-or-cache-url" }`. The client requests this only when the panorama tab is opened,
using a viewport-derived fast preview up to 2048px, a cache-stable 4096px high-quality preview, or
an explicitly selected 8192px preview. The original panorama is never requested by the operator
workspace.

`GET /api/datasets/{id}/points/{frame_id}?budget=120000&radius=40` returns
`application/vnd.mmsp` (or `application/octet-stream`). The browser never requests raw LAS.
`budget` is operator-selectable (60k/120k/250k) and `radius` is 20/40/70 m.

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

The VWorld WebGL 3.0 map view uses a same-origin iframe so the SDK's global viewer lifecycle is
isolated from React. The map view, spherical panorama renderer, and point-cloud renderer are
lazy-loaded; the external VWorld SDK is not bundled by Vite. The shared Three.js dependency is
excluded from the initial application bundle.

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
