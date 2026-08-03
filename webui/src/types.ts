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
  capabilities?: {
    upload?: boolean
    panorama?: boolean
    point_cloud?: boolean
    auto_optimize?: boolean
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
