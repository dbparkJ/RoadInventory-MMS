# SHP results and review-overlay API

Detection runs write into a server-managed directory under
`state_dir/runs/{run_id}/output`. Absolute server paths are intentionally never
returned to a browser. A completed run exposes the logical location and its
downloadable artifacts through:

```http
GET /api/runs/{run_id}/results
```

The response contains `output_location`, ordinary `files`, and a `shapefiles`
array. Each SHP entry has a bundle `download_url` and an `import_url`. The
download endpoint returns the matching `.shp`, `.shx`, `.dbf`, and available
CRS/encoding sidecars as one ZIP. The import endpoint registers that result as
an editable overlay without requiring a browser round trip:

SHP bundles and run/model manifests use a separate bounded priority scan of
`output/shp` and `output/{model}/shp`. Therefore a large crop/image artifact
tree cannot consume the ordinary artifact-page limit before final detection
results are exposed. Result summary endpoints run in the server thread pool so
this filesystem work does not block panorama and map requests.

```http
POST /api/runs/{run_id}/shapefile/import
Content-Type: application/json

{"path":"shp/detected_signs.shp","name":"Detected signs"}
```

## Upload and storage model

```http
POST /api/datasets/{dataset_id}/overlays
Content-Type: multipart/form-data
```

The form accepts `name`, optional `crs`, optional `encoding`, and repeated
`files`. `encoding` may be `auto`, `UTF-8`, `CP949`, or `EUC-KR`; omitted means
`auto`. `files` may be a single ZIP or one complete, same-stem SHP bundle.
`.shp`, `.shx`, and `.dbf` are required; `.prj`, `.qpj`, `.wkt2`, and `.cpg`
are supported. If no CRS is supplied and no CRS sidecar exists, the dataset CRS
is used and a warning is returned.

In automatic encoding mode, `.cpg` wins. If it is absent, DBF record bytes are
strictly checked as UTF-8 and otherwise CP949. The inferred choice is returned
as `source_encoding` together with a warning so an operator can retry with an
explicit choice if labels look wrong. Decoding never uses replacement
characters. Edited downloads use the same encoding and emit a matching `.cpg`;
attribute edits are rejected if a value cannot be encoded or exceeds the DBF
field's byte width. Run-result import accepts the same optional `encoding`
property in its JSON body.

Uploaded and run-result source files are copied into a layer-specific directory
below `state_dir/overlays` and are never modified. Editable geometries and
attributes live in a separate SQLite feature store. Every edit increments a
revision and writes an audit record. Edited downloads are generated from that
store in the dataset CRS; the original bundle remains unchanged.

ZIP paths, symbolic links, encryption, duplicate flattened names, excessive
compression ratios, file counts, decompressed sizes, feature counts, and unsafe
result paths are rejected before registration. All list endpoints are bounded.

## Layer and feature access

```http
GET /api/datasets/{dataset_id}/overlays
GET /api/datasets/{dataset_id}/overlays/{layer_id}
GET /api/datasets/{dataset_id}/overlays/{layer_id}/features?coordinate_space=wgs84
GET /api/datasets/{dataset_id}/overlays/{layer_id}/features?coordinate_space=dataset
GET /api/datasets/{dataset_id}/overlays/{layer_id}/features/{feature_id}?coordinate_space=dataset
```

The feature response is a paginated GeoJSON `FeatureCollection` with `fields`,
`revision`, `total`, and `next_offset`. WGS84 is intended for the map. Dataset
coordinates retain a Point Z value when present and are intended for panorama
and 3D views.

For frame-local 3D loading, the collection endpoint also accepts dataset-space
`center_x`, `center_y`, and `radius` together. SQLite filters indexed Point
coordinates before pagination and returns matching points in distance order;
`total`, `next_offset`, and `spatial_filter` describe that filtered result.
This avoids downloading a large layer's unrelated first pages. The single
feature endpoint returns `{feature, revision, coordinate_space, crs, fields}`
so a panorama selection outside the current page can still open its editor.

Point coordinates and existing DBF attributes can be corrected together or
separately. Unknown attributes and non-Point geometry edits are rejected.
`expected_revision` provides optimistic concurrency protection:

```http
PATCH /api/datasets/{dataset_id}/overlays/{layer_id}/features/{feature_id}
Content-Type: application/json

{
  "geometry":{"type":"Point","coordinates":[302000.1,4120000.2,31.4]},
  "coordinate_space":"dataset",
  "properties":{"CLASS":"pole"},
  "expected_revision":4
}
```

```http
DELETE /api/datasets/{dataset_id}/overlays/{layer_id}/features/{feature_id}?expected_revision=5
GET /api/datasets/{dataset_id}/overlays/{layer_id}/download
DELETE /api/datasets/{dataset_id}/overlays/{layer_id}
```

Feature deletion is a tombstone in the editable store. It does not remove a
record from the preserved source bundle. Removing a whole overlay likewise
only unregisters it from the workspace; its source bundle and audit database
are atomically moved to `state_dir/overlay_archive` so removed layers cannot
clutter the active-layer registry. They remain available for an administrator's
retention or recovery workflow; no browser restore endpoint is exposed yet.

## Panorama and 3D coordinate linkage

Frame list entries expose `dataset_position: [x,y,z]`. Point overlays can be
projected with the same calibrated panorama axes used by MMS point previews:

```http
GET /api/datasets/{dataset_id}/overlays/{layer_id}/project/{frame_id}
```

Each returned item contains normalized equirectangular `u`, `v`, radial
`depth`, `dataset_position`, attributes, and a `z_inferred` flag. A 2-D SHP
point uses the current frame altitude for visualization and is explicitly
marked as inferred.

To turn a selected MMS panorama-point sample back into an editable SHP value:

```http
POST /api/datasets/{dataset_id}/frames/{frame_id}/panorama-pick
Content-Type: application/json

{"u":0.51,"v":0.48,"depth":12.6}
```

The response contains both `dataset_position` and WGS84 `lon`, `lat`, and
`altitude`. Optional yaw/pitch offsets are accepted by both projection APIs;
when omitted, the server's validated panorama-alignment defaults are used.
