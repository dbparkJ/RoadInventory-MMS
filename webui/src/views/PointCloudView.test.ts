import { describe, expect, it } from 'vitest'
import * as THREE from 'three'
import {
  capturePointCloudViewState,
  captureHeadingDirection,
  closestPointHitIndex,
  DEFAULT_POINT_CLOUD_BUDGET,
  datasetPointToFrameLocal,
  demoPanoramaProjectionMetadata,
  pointCloudDetectionPointSize,
  pointCloudBudgetsForMaximum,
  pointCloudDetectionWireframePositions,
  pointCloudDetectionsFromObservations,
  pointCloudHoverState,
  POINT_CLOUD_BUDGETS,
  restorePointCloudViewState,
  type RenderOverlayPoint,
} from './PointCloudView'

describe('PointCloudView camera continuity', () => {
  it('uses 250k as the minimum and offers 500k and 1m previews', () => {
    expect(DEFAULT_POINT_CLOUD_BUDGET).toBe(250_000)
    expect(POINT_CLOUD_BUDGETS.map((entry) => entry.value)).toEqual([
      250_000,
      500_000,
      1_000_000,
    ])
    expect(POINT_CLOUD_BUDGETS.at(-1)?.label).toContain('100만')
    expect(pointCloudBudgetsForMaximum(250_000).map((entry) => entry.value)).toEqual([
      250_000,
    ])
    expect(pointCloudBudgetsForMaximum(500_000).at(-1)?.value).toBe(500_000)
    expect(pointCloudBudgetsForMaximum(1_000_000).at(-1)?.value).toBe(1_000_000)
  })

  it('restores camera angle, orbit target, and zoom for the next frame', () => {
    const camera = new THREE.PerspectiveCamera(52, 1, 0.05, 2_000)
    const target = new THREE.Vector3(-1.25, 4.5, 2.1)
    camera.position.set(14.2, -18.4, 9.8)
    camera.zoom = 1.7
    const saved = capturePointCloudViewState(camera, target)

    camera.position.set(1, 2, 3)
    camera.zoom = 1
    target.set(0, 0, 0)
    restorePointCloudViewState(camera, target, saved)

    expect(camera.position.toArray()).toEqual([14.2, -18.4, 9.8])
    expect(target.toArray()).toEqual([-1.25, 4.5, 2.1])
    expect(camera.zoom).toBe(1.7)
  })

  it('converts an absolute SHP point into the current MMSP frame coordinates', () => {
    expect(datasetPointToFrameLocal([1007.5, 2012, 31.25], [1000, 2000, 30])).toEqual([
      7.5,
      12,
      1.25,
    ])
    expect(datasetPointToFrameLocal([1007.5, 2012], [1000, 2000, 30])).toEqual([7.5, 12, 0])
  })

  it('converts a north-based GNSS heading into the local east/north direction', () => {
    expect(captureHeadingDirection(0)).toEqual([0, 1, 0])
    const east = captureHeadingDirection(90)
    expect(east[0]).toBeCloseTo(1)
    expect(east[1]).toBeCloseTo(0)
  })

  it('keeps detected SHP points visibly larger than the previous sub-metre marker', () => {
    expect(pointCloudDetectionPointSize(false)).toBeGreaterThan(0.5)
    expect(pointCloudDetectionPointSize(true)).toBeGreaterThan(pointCloudDetectionPointSize(false))
  })

  it('renders only accepted finite YOLO 3D positions within 25m and preserves SHP details', () => {
    const overlayPoint: RenderOverlayPoint = {
      layerId: 'layer-1',
      layerName: 'Detected signs',
      featureId: 'feature-7',
      color: '#22c55e',
      position: [10.1, 0, 2],
      selected: true,
      properties: {
        det_id: 'DET-1',
        model_nm: 'model-a.pt',
        img_name: 'frame-1.jpg',
        class_nm: 'sign',
        asset_id: 'asset-7',
      },
    }
    const points = pointCloudDetectionsFromObservations([
      {
        source_id: 'source-a',
        source_name: 'model-a.pt',
        observation_id: 'det-1',
        dataset_position: [1010, 2000, 32],
        properties: {
          det_id: 'det-1',
          model_nm: 'model-a.pt',
          img_name: 'frame-1.jpg',
          class_nm: 'sign',
        },
      },
      {
        source_id: 'source-b',
        observation_id: 'det-far',
        dataset_position: [1030, 2000, 30],
        properties: { det_id: 'det-far' },
      },
      {
        source_id: 'source-c',
        observation_id: 'det-unlocated',
        properties: { det_id: 'det-unlocated' },
      },
    ], [1000, 2000, 30], [overlayPoint])

    expect(points).toHaveLength(1)
    expect(points[0]).toMatchObject({
      position: [10, 0, 2],
      layerId: 'layer-1',
      featureId: 'feature-7',
      layerName: 'Detected signs',
      selected: true,
      properties: { asset_id: 'asset-7' },
    })
    expect(points[0].tooltipColor).toBe('#22c55e')
  })

  it('does not link Details when same det_id has incompatible image or class metadata', () => {
    const representative: RenderOverlayPoint = {
      layerId: 'layer-1',
      layerName: 'Detected signs',
      featureId: 'feature-7',
      color: '#22c55e',
      position: [10, 0, 2],
      selected: false,
      properties: {
        det_id: 'det-1',
        model_nm: 'model-a.pt',
        img_name: 'other-frame.jpg',
        class_nm: 'traffic_light',
      },
    }
    const points = pointCloudDetectionsFromObservations([{
      source_id: 'source-a',
      observation_id: 'det-1',
      dataset_position: [1010, 2000, 32],
      properties: {
        det_id: 'det-1',
        model_nm: 'model-a.pt',
        img_name: 'frame-1.jpg',
        class_nm: 'traffic_sign',
      },
    }], [1000, 2000, 30], [representative])

    expect(points).toHaveLength(1)
    expect(points[0].layerId).toBeUndefined()
    expect(points[0].featureId).toBeUndefined()
  })

  it('keeps one model color even when observations come from different source paths', () => {
    const observations = ['run-a/model.pt', 'run-b/model.pt'].map((sourceId, index) => ({
      source_id: sourceId,
      model_id: 'model-stable-id',
      observation_id: `det-${index}`,
      dataset_position: [1001 + index, 2000, 30] as [number, number, number],
      properties: { class_nm: 'traffic_sign' },
    }))

    const points = pointCloudDetectionsFromObservations(observations, [1000, 2000, 30], [])

    expect(points).toHaveLength(2)
    expect(points[0].color).toBe(points[1].color)
  })

  it('does not drop accepted detections after the first 512 observations', () => {
    const observations = Array.from({ length: 600 }, (_, index) => ({
      source_id: 'source-a',
      observation_id: `det-${index}`,
      dataset_position: [1001 + (index % 5), 2000, 30] as [number, number, number],
      properties: { det_id: `det-${index}` },
    }))
    expect(
      pointCloudDetectionsFromObservations(observations, [1000, 2000, 30], []),
    ).toHaveLength(600)
  })

  it('batches every detection cube into one line-segment position buffer', () => {
    const positions = pointCloudDetectionWireframePositions([
      {
        sourceId: 'source-a',
        observationId: 'det-1',
        layerName: 'YOLO',
        color: '#ffb84d',
        position: [10, 20, 30],
        selected: false,
        properties: {},
      },
      {
        sourceId: 'source-b',
        observationId: 'det-2',
        layerName: 'YOLO',
        color: '#4dd9ff',
        position: [0, 0, 0],
        selected: true,
        properties: {},
      },
    ])

    expect(positions).toHaveLength(2 * 12 * 2 * 3)
    expect(Math.min(...positions.slice(0, 12 * 2 * 3))).toBeCloseTo(9.65)
    expect(Math.max(...positions.slice(0, 12 * 2 * 3))).toBeCloseTo(30.35)
    expect(Math.min(...positions.slice(12 * 2 * 3))).toBeCloseTo(-0.45)
    expect(Math.max(...positions.slice(12 * 2 * 3))).toBeCloseTo(0.45)
  })

  it('preserves model and SHP layer colors in transient and pinned hover state', () => {
    const viewport = { x: 10, y: 20, viewportWidth: 800, viewportHeight: 600 }
    const detection = pointCloudHoverState({
      sourceId: 'model-a',
      observationId: 'det-1',
      layerName: 'YOLO · model-a',
      color: '#4dd9ff',
      position: [1, 2, 3],
      selected: false,
      properties: { class_nm: 'traffic_sign' },
    }, viewport)
    const overlay = pointCloudHoverState({
      layerId: 'layer-1',
      layerName: '표지 레이어',
      featureId: 'feature-7',
      color: '#22c55e',
      position: [1, 2, 3],
      selected: false,
      properties: { class_nm: 'traffic_sign' },
    }, viewport)

    expect(detection).toMatchObject({
      featureId: 'det-1',
      layerColor: '#4dd9ff',
      ...viewport,
    })
    expect(overlay).toMatchObject({
      layerId: 'layer-1',
      featureId: 'feature-7',
      layerColor: '#22c55e',
      ...viewport,
    })

    expect(pointCloudHoverState({
      sourceId: 'model-a',
      observationId: 'det-linked',
      layerId: 'layer-1',
      layerName: '표지 레이어',
      featureId: 'feature-7',
      color: '#4dd9ff',
      tooltipColor: '#22c55e',
      position: [1, 2, 3],
      selected: false,
      properties: {},
    }, viewport)).toMatchObject({ layerColor: '#22c55e' })
  })

  it('provides deterministic calibrated axes for demo cross-view hover', () => {
    const metadata = demoPanoramaProjectionMetadata({
      id: 'demo-frame',
      index: 0,
      track_id: 'demo-track',
      timestamp: '2026-01-01T00:00:00Z',
      coordinate: null,
      dataset_position: [10, 20, 30],
      has_panorama: true,
      has_points: true,
    })
    expect(metadata).toMatchObject({
      frame_id: 'demo-frame',
      origin: [10, 20, 30],
      forward: [0, 1, 0],
      right: [1, 0, 0],
      up: [0, 0, 1],
    })
  })

  it('selects the point closest to the pointer instead of the first depth-sorted hit', () => {
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100)
    camera.position.set(0, 0, 0)
    camera.lookAt(0, 0, -1)
    camera.updateProjectionMatrix()
    camera.updateMatrixWorld(true)
    const nearerButOffset = new THREE.Vector3(0.12, 0, -5)
    const fartherUnderPointer = new THREE.Vector3(0.005, 0, -10)

    expect(
      closestPointHitIndex(
        [
          { index: 3, point: nearerButOffset, distance: 5 },
          { index: 9, point: fartherUnderPointer, distance: 10 },
        ],
        { x: 0, y: 0 },
        camera,
        800,
        800,
        20,
      ),
    ).toBe(9)
  })

  it('reprojects the actual Points vertex instead of Three ray closest-point intersections', () => {
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100)
    camera.position.set(0, 0, 0)
    camera.lookAt(0, 0, -1)
    camera.updateProjectionMatrix()
    camera.updateMatrixWorld(true)
    const geometry = new THREE.BufferGeometry()
    // Vertex 0 is closer in depth but roughly 6 px from the pointer. Vertex 1
    // is farther away but only roughly 1 px from the pointer.
    geometry.setAttribute(
      'position',
      new THREE.Float32BufferAttribute([
        0.0433, 0, -5,
        0.0144, 0, -10,
      ], 3),
    )
    const points = new THREE.Points(geometry, new THREE.PointsMaterial())
    points.updateMatrixWorld(true)
    const raycaster = new THREE.Raycaster()
    raycaster.params.Points!.threshold = 0.18
    raycaster.setFromCamera(new THREE.Vector2(0, 0), camera)
    const intersections = raycaster.intersectObject(points, false)

    expect(intersections.map((intersection) => intersection.index)).toEqual([0, 1])
    // Three reports both Intersection.point values on the ray, so projecting
    // those values alone cannot distinguish their real screen positions.
    expect(intersections[0].point.x).toBeCloseTo(0)
    expect(intersections[1].point.x).toBeCloseTo(0)
    expect(
      closestPointHitIndex(intersections, { x: 0, y: 0 }, camera, 800, 800, 7),
    ).toBe(1)

    geometry.dispose()
    ;(points.material as THREE.Material).dispose()
  })

  it('rejects a ray candidate outside the visible hover radius', () => {
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100)
    camera.lookAt(0, 0, -1)
    camera.updateProjectionMatrix()
    camera.updateMatrixWorld(true)
    expect(
      closestPointHitIndex(
        [{ index: 4, point: new THREE.Vector3(0.5, 0, -5), distance: 5 }],
        { x: 0, y: 0 },
        camera,
        800,
        800,
        7,
      ),
    ).toBeNull()
  })
})
