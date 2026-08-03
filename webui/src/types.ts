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
  timestamp: string
  coordinate: Coordinate | null
  heading?: number
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
  heading?: number
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

export interface BootstrapResponse {
  api_version: string
  server_name?: string
  map_style_url?: string
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
  parameters?: ManualParameters
  auto?: {
    preset: AutoPreset
  }
}

export interface RunRecord {
  id: string
  dataset_id: string
  dataset_name?: string
  status: RunStatus
  progress: number
  stage?: string
  message?: string
  error?: string
  created_at: string
  started_at?: string
  finished_at?: string
  eta_seconds?: number
  result_url?: string
}

export interface RunResultFile {
  path: string
  name: string
  size: number
  type: string
  url: string
}

export interface RunResultShapefile {
  path: string
  name: string
  files?: string[]
  download_url: string
  import_url?: string
}

export interface RunOutputLocation {
  kind: 'server_managed'
  relative_path: string
  results_url: string
}

export interface RunResults {
  files: RunResultFile[]
  file_count: number
  truncated?: boolean
  output_location?: RunOutputLocation
  shapefiles?: RunResultShapefile[]
  feature_counts?: Record<string, number>
  status?: string
}

export type OverlayCoordinateSpace = 'wgs84' | 'dataset'
export type OverlayEncoding = 'auto' | 'UTF-8' | 'CP949' | 'EUC-KR'

export interface OverlayLayer {
  id: string
  dataset_id: string
  name: string
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
