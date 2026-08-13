import type {
  BootstrapResponse,
  DatasetDetail,
  Frame,
  FramePage,
  PointCloudPayload,
  RouteResponse,
  RunRecord,
} from '../types'

const DATASET_ID = 'demo-seoul-01'
const TRACK_IDS = ['track-a', 'track-b']
const START_TIME = Date.UTC(2026, 6, 29, 0, 12, 0)

const buildCoordinate = (index: number) => {
  const progress = index / 119
  return {
    lon: 126.9742 + progress * 0.019 + Math.sin(progress * Math.PI * 4) * 0.0012,
    lat: 37.5651 + progress * 0.0105 + Math.cos(progress * Math.PI * 3) * 0.0008,
    altitude: 28 + Math.sin(progress * Math.PI * 5) * 3,
  }
}

export const demoFrames: Frame[] = Array.from({ length: 120 }, (_, index) => ({
  id: `demo-frame-${String(index + 1).padStart(4, '0')}`,
  index,
  track_id: index < 72 ? TRACK_IDS[0] : TRACK_IDS[1],
  timestamp: new Date(START_TIME + index * 500).toISOString(),
  coordinate: buildCoordinate(index),
  heading: (38 + index * 1.9) % 360,
  speed_kph: 32 + Math.sin(index / 7) * 9,
  has_panorama: true,
  has_points: true,
}))

export const demoDataset: DatasetDetail = {
  id: DATASET_ID,
  name: '서울 도심 MMS · 데모',
  relative_path: 'demo/seoul_2026_07_29',
  status: 'ready',
  frame_count: demoFrames.length,
  point_count: 86_420_000,
  distance_m: 4_820,
  size_bytes: 18_620_000_000,
  crs: 'EPSG:5186',
  captured_at: new Date(START_TIME).toISOString(),
  indexed_at: new Date(START_TIME + 3_600_000).toISOString(),
  bounds: [126.972, 37.563, 126.996, 37.578],
  sensors: { lidar: 'Velodyne Alpha Prime', camera_count: 6, gnss: 'Applanix POS LV' },
  tracks: [
    {
      id: TRACK_IDS[0],
      name: '구간 A · 세종대로',
      frame_count: 72,
      distance_m: 2_940,
      captured_at: new Date(START_TIME).toISOString(),
    },
    {
      id: TRACK_IDS[1],
      name: '구간 B · 을지로',
      frame_count: 48,
      distance_m: 1_880,
      captured_at: new Date(START_TIME + 36_000).toISOString(),
    },
  ],
}

export const demoBootstrap: BootstrapResponse = {
  api_version: 'demo',
  server_name: '로컬 데모',
  map: {
    provider: 'vworld',
    engine: 'webgl',
    version: '3.0',
  },
  datasets: [demoDataset],
  capabilities: {
    upload: true,
    panorama: true,
    point_cloud: true,
    max_point_budget: 1_000_000,
    auto_optimize: true,
  },
  recent_runs: [
    {
      id: 'demo-run-done',
      dataset_id: DATASET_ID,
      dataset_name: demoDataset.name,
      status: 'completed',
      progress: 100,
      stage: '결과 패키징',
      created_at: new Date(Date.now() - 7_200_000).toISOString(),
      finished_at: new Date(Date.now() - 6_930_000).toISOString(),
    },
  ],
}

export const demoRoute: RouteResponse = {
  points: demoFrames.flatMap((frame) =>
    frame.coordinate
      ? [
          {
            ...frame.coordinate,
            frame_id: frame.id,
            track_id: frame.track_id,
            heading: frame.heading,
          },
        ]
      : [],
  ),
}

export function getDemoFrames(
  offset: number,
  limit: number,
  track?: string,
): FramePage {
  const source = track ? demoFrames.filter((frame) => frame.track_id === track) : demoFrames
  const items = source.slice(offset, offset + limit)
  return {
    items,
    offset,
    limit,
    total: source.length,
    next_offset: offset + items.length < source.length ? offset + items.length : null,
  }
}

export function createDemoPointCloud(budget: number): PointCloudPayload {
  // Mirror the real preview selector so the 25만/50만/100만 density options remain
  // meaningful in demo mode as well.
  const pointCount = Math.min(Math.max(250_000, budget), 1_000_000)
  const positions = new Float32Array(pointCount * 3)
  const colors = new Uint8Array(pointCount * 3)

  for (let index = 0; index < pointCount; index += 1) {
    const offset = index * 3
    const lane = index % 7
    const bandRatio = index / pointCount
    const bandPhase = (index * 0.61803398875) % 1
    const longitudinal = bandRatio < 0.75
      ? bandPhase * 30 - 15
      : (bandPhase < 0.5 ? -1 : 1) * (15 + (bandPhase % 0.5) * 20)
    const lateralNoise = Math.sin(index * 12.9898) * 0.16
    let x = (lane - 3) * 1.7 + lateralNoise
    let y = longitudinal
    let z = Math.sin(longitudinal / 16) * 0.2
    let color: [number, number, number] = [102, 133, 153]

    if (lane === 0 || lane === 6) {
      z = ((index * 0.754877) % 1) * 8
      x += lane === 0 ? -2.5 : 2.5
      color = z > 4 ? [62, 137, 113] : [91, 112, 104]
    } else if (lane === 2 || lane === 4) {
      z += 0.025
      color = [219, 197, 107]
    } else {
      color = [83 + (index % 38), 108 + (index % 34), 125 + (index % 31)]
    }

    positions[offset] = x
    positions[offset + 1] = y
    positions[offset + 2] = z
    colors[offset] = color[0]
    colors[offset + 1] = color[1]
    colors[offset + 2] = color[2]
  }

  return {
    positions,
    colors,
    bounds: { min: [-9, -25, -1], max: [9, 25, 9] },
    pointCount,
  }
}

export function createDemoRun(sequence: number): RunRecord {
  return {
    id: `local-demo-${Date.now()}-${sequence}`,
    dataset_id: DATASET_ID,
    dataset_name: demoDataset.name,
    status: 'running',
    progress: 7,
    stage: '데이터 준비',
    message: '데모 실행은 브라우저 안에서 진행됩니다.',
    created_at: new Date().toISOString(),
    started_at: new Date().toISOString(),
    eta_seconds: 92,
  }
}
