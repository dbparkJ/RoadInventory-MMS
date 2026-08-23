export type ViewMode = 'map' | 'panorama' | 'pointcloud'
export type ParameterMode = 'manual' | 'automatic'
export type FrameRange = [number, number]
export type RunStatus =
  | 'queued'
  | 'preparing'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'cancelling'

export type CanonicalRunStatus =
  | 'pending'
  | 'validating'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'retrying'
  | 'cancelled'

export interface RunErrorInfo {
  code: string
  message: string
  stage: string
  job_id: string
  retryable: boolean
  object_id?: string | null
  context?: Record<string, unknown>
  cause_type?: string | null
}

export interface RunStageResult {
  stage_name: string
  stage_version?: string
  status: string
  started_at?: string
  finished_at?: string | null
  elapsed_ms?: number | null
  input_count?: number
  output_count?: number
  rejected_count?: number
  metrics?: Record<string, number | string>
  warnings?: unknown[]
}

export interface RunVersions {
  [name: string]: unknown
  git_commit?: string | null
  model?: unknown
  model_hashes?: Record<string, string>
  config_hash?: string | null
  config_schema?: number | string | null
  calibration_id?: string | string[] | null
  calibration_hash?: string | string[] | null
}

export type RunCounts = Record<string, number>

export interface Coordinate {
  lon: number
  lat: number
  altitude?: number
}

export interface Track {
  id: string
  name: string
  frame_count: number
  distance_m?: number
  captured_at?: string
}

export interface DatasetSummary {
  id: string
  name: string
  relative_path?: string
  status: 'ready' | 'indexing' | 'error'
  frame_count: number
  point_count?: number
  distance_m?: number
  size_bytes?: number
  crs?: string
  captured_at?: string
  tracks: Track[]
  thumbnail_url?: string
}

export interface DatasetDetail extends DatasetSummary {
  sensors?: {
    lidar?: string
    camera_count?: number
    gnss?: string
  }
  bounds?: [number, number, number, number]
  indexed_at?: string
}

export interface Frame {
  id: string
  index: number
  track_id: string
  image_name?: string
  timestamp: string
  coordinate: Coordinate | null
  heading?: number | null
  speed_kph?: number
  has_panorama: boolean
  has_points: boolean
  thumbnail_url?: string
  dataset_position?: [number, number, number]
}

export interface RoutePoint extends Coordinate {
  frame_id?: string
  track_id?: string
  index?: number
  heading?: number | null
}

export interface RouteResponse {
  type?: 'Feature' | 'FeatureCollection'
  points: RoutePoint[]
}

export interface StorageRoot {
  id: string
  name: string
  path_hint?: string
  writable: boolean
  free_bytes?: number
}

export interface StorageEntry {
  name: string
  relative_path: string
  type: 'directory' | 'file'
  size_bytes?: number
  modified_at?: string
  dataset_hint?: boolean
}

export interface StorageTreeResponse {
  root_id: string
  relative_path: string
  entries: StorageEntry[]
  truncated?: boolean
}

export interface MapProviderMetadata {
  provider: 'vworld'
  engine: 'webgl'
  version: '3.0'
}

export interface BootstrapResponse {
  api_version: string
  server_name?: string
  map: MapProviderMetadata
  datasets: DatasetSummary[]
  recent_runs?: RunRecord[]
  preview_defaults?: {
    panorama_point_yaw_offset_deg?: number
    panorama_point_pitch_offset_deg?: number
    panorama_point_budget?: number
    panorama_point_radius_m?: number
    panorama_point_cell_size_px?: number
  }
  capabilities?: {
    upload?: boolean
    panorama?: boolean
    point_cloud?: boolean
    auto_optimize?: boolean
    panorama_point_overlay?: boolean
    shp_overlays?: boolean
    shp_feature_editing?: boolean
    shp_result_download?: boolean
    pole_base_inference?: boolean
    max_point_budget?: number
    max_overlay_upload_bytes?: number
    max_overlay_features?: number
  }
}

export interface FramePage {
  items: Frame[]
  offset: number
  limit: number
  total: number
  next_offset?: number | null
}

export interface ManualParameters {
  voxel_size: number
  confidence: number
  cluster_distance: number
  min_points: number
  search_radius: number
  ground_tolerance: number
}

export type AutoPreset = 'fast' | 'balanced' | 'precise'

export interface RunRequest {
  dataset_id: string
  track_ids: string[]
  frame_range: FrameRange | null
  mode: ParameterMode
  run_name?: string
  layer_name?: string
  model_names?: string[]
  parameters?: ManualParameters
  auto?: {
    preset: AutoPreset
  }
}

export interface RunRecord {
  id: string
  name?: string | null
  job_id?: string
  dataset_id: string
  dataset_name?: string
  status: RunStatus
  canonical_status?: CanonicalRunStatus
  attempt?: number
  manifest_schema_version?: number
  progress: number
  stage?: string
  current_stage?: string | null
  message?: string
  error?: string
  error_info?: RunErrorInfo | null
  versions?: RunVersions
  counts?: RunCounts
    stage_results?: RunStageResult[]
    created_at: string
    updated_at?: string
    started_at?: string
    finished_at?: string
  eta_seconds?: number
  result_url?: string
  request?: RunRequest
  resolved?: Record<string, unknown>
}

export interface SurveySegment {
  id: string
  dataset_id: string
  name: string
  color: string
  geometry: {
    type: 'LineString'
    coordinates: [number, number][]
  }
  created_at: string
  updated_at: string
}

export interface RunResultFile {
  path: string
  name: string
  size: number
  type: string
  url: string
}

export interface DetectionModelOption {
  id: string
  name: string
  label: string
}

export interface RunResultShapefile {
  path: string
  name: string
  display_name?: string
  files?: string[]
  download_url: string
  import_url?: string
}

export interface RunOutputLocation {
  kind: 'server_managed'
  relative_path: string
  results_url: string
}

export interface RunArchiveLink {
  url: string
  filename: string
}

export interface RunArchives {
  all: RunArchiveLink
  detected_images: RunArchiveLink
}

export interface RunResults {
  files: RunResultFile[]
  file_count: number
  truncated?: boolean
  output_location?: RunOutputLocation
  shapefiles?: RunResultShapefile[]
  archives?: RunArchives
  feature_counts?: Record<string, number>
  status?: string
}

export type OverlayCoordinateSpace = 'wgs84' | 'dataset'
export type OverlayEncoding = 'auto' | 'UTF-8' | 'CP949' | 'EUC-KR'

export interface OverlayLayer {
  id: string
  dataset_id: string
  name: string
  color?: string | null
  metadata_revision?: number
  source_kind?: string
  source_crs?: string
  dataset_crs?: string
  map_crs?: string
  source_encoding?: string
  edited_download_encoding?: string
  geometry_type: string
  shape_type?: number
  feature_count: number
  original_feature_count?: number
  revision: number
  fields?: OverlayField[]
  warnings?: string[]
  features_url?: string
  project_url_template?: string
  download_url?: string
  source_preserved?: boolean
  created_at?: string
}

export interface OverlayField {
  name: string
  type?: string
  size?: number
  decimal?: number
  required?: boolean
  internal?: boolean
}

export interface OverlayFieldDeleteResponse {
  deleted_field: string
  revision: number
  fields: OverlayField[]
  layer: OverlayLayer
  source_preserved: boolean
}

export interface OverlayGeometry {
  type: string
  coordinates: unknown
}

export interface OverlayFeature {
  type: 'Feature'
  id: string | number
  geometry: OverlayGeometry | null
  properties: Record<string, unknown>
}

export interface OverlayFeatureCreateRequest {
  geometry?: { type: 'Point'; coordinates: [number, number, number?] }
  coordinate_space?: OverlayCoordinateSpace
  copy_geometry_from?: string | number
  expected_revision?: number
  properties?: Record<string, unknown>
}

export type PoleBaseInferStatus = 'auto' | 'review' | 'failed'

export interface PoleBaseInferRequest {
  coordinate_space: 'dataset'
  seed_position: [number, number, number]
  profile: 'balanced'
  debug?: boolean
}

export interface PoleBaseAxisResult {
  point: [number, number, number]
  direction: [number, number, number]
  point_count: number
  observed_z_min: number
  observed_z_max: number
  vertical_span_m: number
  vertical_bin_count: number
  longest_consecutive_bin_count: number
  occupancy_ratio: number
  rmse_m: number
  tilt_deg: number
  seed_distance_m: number
}

export interface PoleBaseGroundResult {
  method: string
  z_at_base: number
  rmse_m: number
  cell_count: number
  candidate_cell_count: number
  nearest_support_distance_m: number
  plane_coefficients: [number, number, number]
  reference_xy: [number, number]
}

export interface PoleBaseQualityComponents {
  seed: number
  axis: number
  span: number
  continuity: number
  ground: number
  bottom_gap: number
}

export interface PoleBaseQualityResult {
  score: number
  candidate_count: number
  ambiguous: boolean
  bottom_gap_m: number | null
  components: PoleBaseQualityComponents
}

export interface PoleBaseInferResponse {
  status: PoleBaseInferStatus
  algorithm: string
  algorithm_version: string
  coordinate_space: 'dataset'
  seed_position: [number, number, number]
  snapped_seed_position?: [number, number, number] | null
  base_position: [number, number, number] | null
  axis?: PoleBaseAxisResult | null
  ground?: PoleBaseGroundResult | null
  quality: PoleBaseQualityResult
  reason_codes: string[]
  warnings: string[]
  debug?: Record<string, unknown> | null
}

export interface OverlayFeatureCollection {
  type: 'FeatureCollection'
  features: OverlayFeature[]
  fields: OverlayField[]
  total: number
  offset: number
  limit: number
  crs?: string
  revision: number
  next_offset?: number | null
  spatial_filter?: {
    coordinate_space: 'dataset'
    center: [number, number]
    radius: number
    geometry_type: 'Point'
  } | null
}

export interface OverlayFeatureDetail {
  feature: OverlayFeature
  revision: number
  coordinate_space: OverlayCoordinateSpace
  crs: string
  fields: OverlayField[]
}

export interface PanoramaOverlayFeature {
  feature_id: string | number
  u: number
  v: number
  depth: number
  dataset_position: [number, number, number]
  z_inferred?: boolean
  properties?: Record<string, unknown>
}

export interface PanoramaDetectionBoxObservation {
  source_id: string
  model_id?: string
  source_name?: string
  observation_id: string
  feature_id?: string | number
  dataset_position?: [number, number, number]
  properties: Record<string, unknown>
}

export interface PanoramaDetectionModel {
  source_id: string
  model_id?: string
  source_name?: string
  count: number
}

export interface FrameDetectionResponse {
  dataset_id: string
  frame_id: string
  coordinate_space: 'panorama_equirectangular_pixels'
  projection: 'equirectangular'
  items: PanoramaDetectionBoxObservation[]
  /** Present on current servers; clients also derive this from items for compatibility. */
  models?: PanoramaDetectionModel[]
  count: number
  model_count: number
  truncated: boolean
}

export interface PanoramaProjectionMetadata {
  frame_id: string
  coordinate_space: 'dataset'
  projection: 'normalized_equirectangular'
  origin: [number, number, number]
  forward: [number, number, number]
  right: [number, number, number]
  up: [number, number, number]
  yaw_offset_deg: number
  pitch_offset_deg: number
}

export interface FrameAddressResponse {
  dataset_id: string
  frame_id: string
  coordinate: Coordinate
  address: string | null
  address_type: string | null
  zipcode: string | null
  source: 'delivery_metadata' | 'vworld' | 'coordinate_fallback'
}

export interface FrameLocateResponse {
  frame: Frame
  page_offset: number
  match: 'image_name' | 'nearest_position'
}

export interface RunEvent {
  type: 'snapshot' | 'progress' | 'stage' | 'completed' | 'failed' | 'cancelled'
  run?: RunRecord
  progress?: number
  stage?: string
  message?: string
  result_url?: string
}

export interface PointCloudPayload {
  positions: Float32Array
  colors: Uint8Array | null
  bounds: {
    min: [number, number, number]
    max: [number, number, number]
  }
  pointCount: number
}

export interface PanoramaPointPayload {
  coordinates: Float32Array
  colors: Uint8Array | null
  pointCount: number
}

export interface UploadManifestFile {
  path: string
  size: number
  type: string
  last_modified: number
}

export interface UploadSession {
  id: string
  chunk_size: number
  files: Array<{
    id: string
    path: string
    size?: number
    uploaded_bytes?: number
  }>
}
