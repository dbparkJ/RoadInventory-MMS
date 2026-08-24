import { describe, expect, it, vi } from 'vitest'
import * as THREE from 'three'
import type { PointCloudPayload, PoleBaseInferResponse } from '../types'
import {
  capturePointCloudViewState,
  captureHeadingDirection,
  closestPointHitIndex,
  applyPointCloudPickedCoordinate,
  buildPointCloudDisplayPayload,
  buildPointCloudSelectionDisplay,
  createPointCloudRenderLoop,
  DEFAULT_POINT_CLOUD_BUDGET,
  datasetPointToFrameLocal,
  demoPanoramaProjectionMetadata,
  mergeNearbyOverlayFeatures,
  pointCloudOverlayPointSize,
  pointCloudMeasurement,
  pointCloudOwnerWindow,
  pointCloudBudgetsForMaximum,
  pointCloudDetectionWireframePositions,
  pointCloudDetectionFocus,
  pointCloudDetectionFocusKey,
  pointCloudDetectionsFromObservations,
  pointCloudExactSupportFocusState,
  pointCloudFocusTransitionProgress,
  pointCloudHoverState,
  pointCloudInfrastructureLayerFocus,
  pointCloudPickTargetAcceptsPoint,
  pointCloudPreviewFocuses,
  pointCloudSelectionFocus,
  pointCloudSelectionFocusForState,
  pointHitIndices,
  nextCycledPointHit,
  pointCloudYoloBoxHalfSize,
  poleBasePreviewGeometry,
  poleBasePrimaryWarning,
  poleBaseStatusLabel,
  POINT_CLOUD_YOLO_HIT_RADIUS_PX,
  POINT_CLOUD_YOLO_MARKER_SIZE,
  POINT_CLOUD_YOLO_RAYCAST_THRESHOLD,
  POINT_CLOUD_BUDGETS,
  restorePointCloudViewState,
  type RenderPointCloudDetection,
  type RenderOverlayPoint,
} from './PointCloudView'

describe('PointCloudView local tools', () => {
  const payload: PointCloudPayload = {
    positions: new Float32Array([
      0, 0, 0,
      5, 0, 1,
      5, 5, 2,
    ]),
    colors: new Uint8Array([
      10, 20, 30,
      40, 50, 60,
      70, 80, 90,
    ]),
    bounds: { min: [0, 0, 0], max: [5, 5, 2] },
    pointCount: 3,
  }

  it('merges exact support matches over paged spatial duplicates', () => {
    const spatial = {
      layerId: 'poles',
      layerName: 'Support poles',
      color: '#111111',
      visible: true,
      feature: {
        type: 'Feature' as const,
        id: 'pole-1',
        geometry: { type: 'Point', coordinates: [1, 2, 0] },
        properties: { support_id: 'stale' },
      },
    }
    const exact = {
      ...spatial,
      color: '#22c55e',
      visible: false,
      feature: { ...spatial.feature, properties: { support_id: 'pole-1' } },
    }

    const merged = mergeNearbyOverlayFeatures([spatial], [exact])

    expect(merged).toHaveLength(1)
    expect(merged[0]).toEqual(exact)
  })

  it('fails closed instead of focusing a partial support lookup', () => {
    expect(pointCloudExactSupportFocusState(null, '', '', 'request-a')).toBe('ready')
    expect(pointCloudExactSupportFocusState('pole-1', '', '', 'request-a')).toBe('pending')
    expect(pointCloudExactSupportFocusState('pole-1', '', 'request-a', 'request-a'))
      .toBe('failed')
    expect(pointCloudExactSupportFocusState('pole-1', 'request-a', '', 'request-a'))
      .toBe('ready')
  })

  it('focuses the selected AI result immediately while exact support data is pending', () => {
    const selected: RenderOverlayPoint = {
      layerId: 'signals',
      layerName: '신호등',
      featureId: 'signal-1',
      color: '#ffb84d',
      position: [2, 3, 8],
      selected: true,
      properties: { det_id: 'det-1', support_id: 'pole-1' },
    }
    const support: RenderOverlayPoint = {
      ...selected,
      layerId: 'poles',
      layerName: '지주',
      featureId: 'pole-1',
      position: [2.2, 3.1, 0],
      selected: false,
      properties: { support_id: 'pole-1', pole_status: 'AUTO' },
    }

    expect(pointCloudSelectionFocusForState([selected, support], 'pending')?.related).toEqual([])
    expect(pointCloudSelectionFocusForState([selected, support], 'failed')?.selected).toBe(selected)
    expect(pointCloudSelectionFocusForState([selected, support], 'ready')?.related).toEqual([support])
  })

  it('eases the selection focus transition into its final state', () => {
    expect(pointCloudFocusTransitionProgress(-1)).toBe(0)
    expect(pointCloudFocusTransitionProgress(0)).toBe(0)
    expect(pointCloudFocusTransitionProgress(130)).toBeCloseTo(0.875)
    expect(pointCloudFocusTransitionProgress(260)).toBe(1)
    expect(pointCloudFocusTransitionProgress(1, 0)).toBe(1)
  })

  it('reuses the bounded MMSP payload while no local geometry filter is active', () => {
    expect(buildPointCloudDisplayPayload(payload, {
      clipRadiusM: null,
      zRange: null,
    })).toBe(payload)
  })

  it('filters around the selected center while preserving server-derived RGB bytes', () => {
    const filtered = buildPointCloudDisplayPayload(payload, {
      clipRadiusM: 1,
      clipCenter: [5, 0],
      zRange: [0.5, 1.5],
    })

    expect(filtered.pointCount).toBe(1)
    expect([...filtered.positions]).toEqual([5, 0, 1])
    expect([...(filtered.colors ?? [])]).toEqual([40, 50, 60])
    expect(filtered.bounds).toEqual({ min: [5, 0, 1], max: [5, 0, 1] })
    expect([...payload.positions]).toEqual([0, 0, 0, 5, 0, 1, 5, 5, 2])
  })

  it('isolates the proposal in 3D and reports 3D/XY/vertical measurements', () => {
    const filtered = buildPointCloudDisplayPayload(payload, {
      clipRadiusM: null,
      proposalPosition: [5, 5, 2],
      isolateProposal: true,
      proposalRadiusM: 0.2,
    })
    expect([...filtered.positions]).toEqual([5, 5, 2])
    expect(pointCloudMeasurement([0, 0, 1], [3, 4, 13])).toEqual({
      distance3d: 13,
      distanceXy: 5,
      vertical: 12,
    })
  })

  it('keeps an AI-selected sign and its support-linked pole as one visual focus', () => {
    const points: RenderOverlayPoint[] = [
      {
        layerId: 'signs',
        layerName: '표지 검출',
        featureId: 'sign-1',
        color: '#ffb84d',
        position: [1, 2, 5],
        selected: true,
        properties: { det_id: 'det-1', support_id: 'POLE-7', class_nm: 'traffic_light' },
      },
      {
        layerId: 'signs',
        layerName: '표지 검출',
        featureId: 'sign-sibling',
        color: '#ffb84d',
        position: [1.1, 2.05, 4.8],
        selected: false,
        properties: { det_id: 'det-2', support_id: 'pole-7', class_nm: 'traffic_sign' },
      },
      {
        layerId: 'poles',
        layerName: '지주 하단',
        featureId: 'pole-7',
        color: '#2bcfa8',
        position: [1.2, 2.1, 0],
        selected: false,
        properties: { support_id: 'pole-7', pole_status: 'AUTO' },
      },
      {
        layerId: 'poles',
        layerName: '지주 하단',
        featureId: 'pole-other',
        color: '#2bcfa8',
        position: [8, 8, 0],
        selected: false,
        properties: { support_id: 'pole-other' },
      },
    ]

    const focus = pointCloudSelectionFocus(points)

    expect(focus?.supportId).toBe('pole-7')
    expect(focus?.related.map((point) => point.featureId)).toEqual(['pole-7'])
    expect(focus?.focalPositions).toEqual([[1, 2, 5], [1.2, 2.1, 0]])
    expect(focus?.guideSegments).toEqual([
      [[1, 2, 5], [1.2, 2.1, 5]],
      [[1.2, 2.1, 5], [1.2, 2.1, 0]],
    ])
    expect(pointCloudPreviewFocuses(focus, [100, 200, 30])).toEqual([
      [101, 202],
      [101.2, 202.1],
    ])
    expect(pointCloudPreviewFocuses(focus, [100, 200, 30], Number.NaN)).toEqual([
      [101, 202],
      [101.2, 202.1],
    ])
    expect(pointCloudSelectionFocus([{
      ...points[0],
      layerName: '기타 객체',
      properties: { source: 'manual' },
    }])).toBeNull()
  })

  it('focuses a selected support pole and links back to its signs and signals', () => {
    const pole: RenderOverlayPoint = {
      layerId: 'poles',
      layerName: '지주 하단',
      featureId: 'pole-7',
      color: '#2bcfa8',
      position: [1.2, 2.1, 0],
      selected: true,
      properties: { support_id: 'pole-7', pole_status: 'AUTO' },
    }
    const sign: RenderOverlayPoint = {
      layerId: 'signs',
      layerName: '교통표지',
      featureId: 'sign-1',
      color: '#ffb84d',
      position: [1, 2, 5],
      selected: false,
      properties: { det_id: 'det-1', support_id: 'POLE-7', class_nm: 'traffic_sign' },
    }

    const focus = pointCloudSelectionFocus([pole, sign])

    expect(focus?.selected).toBe(pole)
    expect(focus?.related).toEqual([sign])
    expect(focus?.guideSegments).toEqual([
      [[1, 2, 5], [1.2, 2.1, 5]],
      [[1.2, 2.1, 5], [1.2, 2.1, 0]],
    ])
  })

  it('focuses a raw detection even when no editable layer or feature is linked', () => {
    const detection: RenderPointCloudDetection = {
      sourceId: 'run-a/model.pt',
      observationId: 'frame-12:0',
      layerName: 'YOLO · model.pt',
      color: '#ffb84d',
      position: [4, 5, 7],
      selected: false,
      properties: {
        class_nm: 'traffic_signal',
        support_id: 'pole-12',
      },
    }
    const pole: RenderOverlayPoint = {
      layerId: 'poles',
      layerName: 'Support poles',
      featureId: 'pole-12',
      color: '#2bcfa8',
      position: [4.5, 5.25, 0],
      selected: false,
      properties: { support_id: 'pole-12', pole_status: 'AUTO' },
    }

    const focus = pointCloudDetectionFocus(detection, [pole])

    expect(detection.layerId).toBeUndefined()
    expect(detection.featureId).toBeUndefined()
    expect(focus?.selected).toBe(detection)
    expect(focus?.related).toEqual([pole])
    expect(focus?.focalPositions).toEqual([[4, 5, 7], [4.5, 5.25, 0]])
    expect(focus?.supportPositions).toEqual([[4.5, 5.25, 0]])
    expect(focus?.guideSegments).toEqual([
      [[4, 5, 7], [4.5, 5.25, 7]],
      [[4.5, 5.25, 7], [4.5, 5.25, 0]],
    ])
  })

  it('separates same-frame raw detections by source, observation, and position', () => {
    const base = {
      sourceId: 'model-a',
      observationId: 'frame-12:0',
      position: [1, 2, 3] as [number, number, number],
    }

    expect(pointCloudDetectionFocusKey(base)).toBe(pointCloudDetectionFocusKey({ ...base }))
    expect(pointCloudDetectionFocusKey(base)).not.toBe(pointCloudDetectionFocusKey({
      ...base,
      sourceId: 'model-b',
    }))
    expect(pointCloudDetectionFocusKey(base)).not.toBe(pointCloudDetectionFocusKey({
      ...base,
      observationId: 'frame-12:1',
    }))
    expect(pointCloudDetectionFocusKey(base)).not.toBe(pointCloudDetectionFocusKey({
      ...base,
      position: [1.01, 2, 3],
    }))
  })

  it('builds one infrastructure-layer focus for signs, signals, vertical poles, and arms', () => {
    const points: RenderOverlayPoint[] = [
      {
        layerId: 'objects',
        layerName: 'Detected traffic signs',
        featureId: 'sign-1',
        color: '#ffb84d',
        position: [0, 0, 5],
        selected: false,
        properties: { class_nm: 'traffic_sign', support_id: 'pole-1' },
      },
      {
        layerId: 'objects',
        layerName: 'Detected traffic signals',
        featureId: 'signal-2',
        color: '#4dd9ff',
        position: [10, 1, 6],
        selected: false,
        properties: { class_nm: 'traffic_signal', support_id: 'pole-2' },
      },
      {
        layerId: 'poles',
        layerName: 'Support poles',
        featureId: 'pole-1',
        color: '#2bcfa8',
        position: [2, 0, 0],
        selected: false,
        properties: { support_id: 'pole-1', pole_type: 'mast_arm' },
      },
      {
        layerId: 'poles',
        layerName: 'Support poles',
        featureId: 'pole-2',
        color: '#2bcfa8',
        position: [11, 1, 0],
        selected: false,
        properties: { support_id: 'pole-2', pole_status: 'AUTO' },
      },
      {
        layerId: 'markings',
        layerName: 'Road markings',
        featureId: 'line-1',
        color: '#ffffff',
        position: [3, 3, 0],
        selected: false,
        properties: { class_nm: 'lane_marking', det_id: 'det-line' },
      },
    ]

    const focus = pointCloudInfrastructureLayerFocus(points)

    expect(focus?.mode).toBe('infrastructure-layer')
    expect(focus?.selected).toBeNull()
    expect(focus?.related.map((point) => point.featureId)).toEqual([
      'sign-1',
      'signal-2',
      'pole-1',
      'pole-2',
    ])
    expect(focus?.supportPositions).toEqual([[2, 0, 0], [11, 1, 0]])
    expect(focus?.guideSegments).toEqual([
      [[0, 0, 5], [2, 0, 5]],
      [[2, 0, 5], [2, 0, 0]],
      [[10, 1, 6], [11, 1, 6]],
      [[11, 1, 6], [11, 1, 0]],
    ])
  })

  it('deduplicates vertical support anchors and rejects focus points beyond the preview', () => {
    const selected: RenderOverlayPoint = {
      layerId: 'signals',
      layerName: '신호등',
      featureId: 'signal-1',
      color: '#ffb84d',
      position: [2, 3, 8],
      selected: true,
      properties: { det_id: 'det-1', support_id: 'pole-1' },
    }
    const focus = pointCloudSelectionFocus([
      selected,
      {
        ...selected,
        layerId: 'poles',
        featureId: 'pole-top',
        position: [2.25, 3.25, 6],
        selected: false,
        properties: { support_id: 'pole-1', pole_status: 'AUTO' },
      },
      {
        ...selected,
        layerId: 'poles',
        featureId: 'pole-base',
        position: [2.25, 3.25, 0],
        selected: false,
        properties: { support_id: 'pole-1', pole_status: 'AUTO' },
      },
      {
        ...selected,
        layerId: 'poles',
        featureId: 'out-of-range',
        position: [26, 0, 0],
        selected: false,
        properties: { support_id: 'pole-1', pole_status: 'AUTO' },
      },
    ])

    expect(pointCloudPreviewFocuses(focus, [1_000, 2_000, 30])).toEqual([
      [1_002, 2_003],
      [1_002.25, 2_003.25],
    ])
  })

  it('preserves dense object and sign-to-pole corridor points while sparsifying context', () => {
    const focus = pointCloudSelectionFocus([
      {
        layerId: 'signs',
        layerName: '표지 검출',
        featureId: 'sign-1',
        color: '#ffb84d',
        position: [0, 0, 4],
        selected: true,
        properties: { det_id: 'det-1', support_id: 'pole-1' },
      },
      {
        layerId: 'poles',
        layerName: '지주 하단',
        featureId: 'pole-1',
        color: '#2bcfa8',
        position: [0, 0, 0],
        selected: false,
        properties: { support_id: 'POLE-1', pole_status: 'AUTO' },
      },
    ])
    const sourcePositions = [
      0.125, 0, 4,
      0.125, 0, 2,
      0.125, 0, 0,
      ...Array.from({ length: 20 }, (_, index) => [5 + index, 6, 1]).flat(),
    ]
    const source: PointCloudPayload = {
      positions: new Float32Array(sourcePositions),
      colors: new Uint8Array(sourcePositions.map((_, index) => index % 255)),
      bounds: { min: [0, 0, 0], max: [24, 6, 4] },
      pointCount: sourcePositions.length / 3,
    }

    const display = buildPointCloudSelectionDisplay(source, focus, {
      focusRadiusM: 0.3,
      corridorRadiusM: 0.2,
      backgroundStride: 1_000_000,
    })

    expect(display.focusPointCount).toBe(3)
    expect(display.backgroundPointCount).toBeGreaterThanOrEqual(1)
    expect(display.backgroundPointCount).toBeLessThan(20)
    expect([...display.payload.positions.slice(0, 9)]).toEqual([
      0.125, 0, 4,
      0.125, 0, 2,
      0.125, 0, 0,
    ])
    expect(display.payload.pointCount).toBe(
      display.focusPointCount + display.backgroundPointCount,
    )
    const restored = buildPointCloudSelectionDisplay(source, null)
    expect(restored.payload).toBe(source)
    expect(restored.focusPointCount).toBe(0)
    expect(restored.backgroundPointCount).toBe(source.pointCount)
  })

  it('keeps the full vertical support axis dense when the sign is horizontally offset', () => {
    const focus = pointCloudSelectionFocus([
      {
        layerId: 'signals',
        layerName: 'Signals',
        featureId: 'signal-1',
        color: '#ffb84d',
        position: [0, 0, 5],
        selected: true,
        properties: { det_id: 'det-1', support_id: 'pole-1' },
      },
      {
        layerId: 'poles',
        layerName: 'Support poles',
        featureId: 'pole-1',
        color: '#2bcfa8',
        position: [2, 0, 0],
        selected: false,
        properties: { support_id: 'pole-1', pole_status: 'AUTO' },
      },
    ])
    const sourcePositions = [
      0.1, 0, 5,
      2.1, 0, 4,
      1, 0, 5,
      1, 0, 2.5,
      8, 8, 1,
    ]
    const source: PointCloudPayload = {
      positions: new Float32Array(sourcePositions),
      colors: null,
      bounds: { min: [0, 0, 0], max: [8, 8, 5] },
      pointCount: sourcePositions.length / 3,
    }

    const display = buildPointCloudSelectionDisplay(source, focus, {
      focusRadiusM: 0.3,
      corridorRadiusM: 0.2,
      backgroundStride: 1_000_000,
    })

    expect(display.focusPointCount).toBe(3)
    expect([...display.payload.positions.slice(0, 9)]).toEqual([
      0.10000000149011612, 0, 5,
      2.0999999046325684, 0, 4,
      1, 0, 5,
    ])
  })
})

describe('PointCloudView camera continuity', () => {
  it('uses the detached canvas owner Window instead of the opener realm', () => {
    const detachedWindow = { name: 'detached-point-cloud' } as unknown as Window
    const host = {
      ownerDocument: { defaultView: detachedWindow } as unknown as Document,
    } as Pick<HTMLElement, 'ownerDocument'>

    expect(pointCloudOwnerWindow(host)).toBe(detachedWindow)
  })

  it('restarts one owner-window RAF chain after inactivity and cleans it up', () => {
    let nextFrame = 0
    let visible = true
    const callbacks = new Map<number, FrameRequestCallback>()
    const requestAnimationFrame = vi.fn((callback: FrameRequestCallback) => {
      nextFrame += 1
      callbacks.set(nextFrame, callback)
      return nextFrame
    })
    const cancelAnimationFrame = vi.fn((handle: number) => {
      callbacks.delete(handle)
    })
    const draw = vi.fn()
    const loop = createPointCloudRenderLoop(
      { requestAnimationFrame, cancelAnimationFrame },
      draw,
      () => visible,
    )

    loop.wake()
    expect(draw).toHaveBeenCalledTimes(1)
    expect(requestAnimationFrame).toHaveBeenCalledTimes(1)
    const firstFrame = requestAnimationFrame.mock.results[0].value
    const firstCallback = callbacks.get(firstFrame)
    expect(firstCallback).toBeDefined()
    callbacks.delete(firstFrame)
    firstCallback?.(16)
    expect(draw).toHaveBeenCalledTimes(2)
    expect(requestAnimationFrame).toHaveBeenCalledTimes(2)

    const pendingBeforeHide = requestAnimationFrame.mock.results[1].value
    visible = false
    loop.stop()
    loop.wake()
    expect(cancelAnimationFrame).toHaveBeenCalledWith(pendingBeforeHide)
    expect(draw).toHaveBeenCalledTimes(2)

    visible = true
    loop.wake()
    expect(draw).toHaveBeenCalledTimes(3)
    const pendingBeforeContextLoss = requestAnimationFrame.mock.results[2].value
    loop.suspend()
    expect(cancelAnimationFrame).toHaveBeenCalledWith(pendingBeforeContextLoss)

    loop.resume()
    expect(draw).toHaveBeenCalledTimes(4)
    const pendingBeforeDispose = requestAnimationFrame.mock.results[3].value
    loop.dispose()
    expect(cancelAnimationFrame).toHaveBeenCalledWith(pendingBeforeDispose)
    loop.wake()
    expect(draw).toHaveBeenCalledTimes(4)
    expect(callbacks.size).toBe(0)
  })

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

  it('keeps SHP points legible while making YOLO object markers and hit areas compact', () => {
    expect(pointCloudOverlayPointSize(false)).toBeGreaterThan(0.5)
    expect(pointCloudOverlayPointSize(true)).toBeGreaterThan(pointCloudOverlayPointSize(false))
    expect(POINT_CLOUD_YOLO_MARKER_SIZE).toBeLessThan(pointCloudOverlayPointSize(false))
    expect(pointCloudYoloBoxHalfSize(false)).toBeLessThan(0.3)
    expect(pointCloudYoloBoxHalfSize(true)).toBeGreaterThan(pointCloudYoloBoxHalfSize(false))
    expect(POINT_CLOUD_YOLO_RAYCAST_THRESHOLD).toBeLessThan(POINT_CLOUD_YOLO_MARKER_SIZE)
    expect(POINT_CLOUD_YOLO_HIT_RADIUS_PX).toBeLessThan(12)
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

  it('uses the server-resolved editable feature even when its layer is hidden', () => {
    const points = pointCloudDetectionsFromObservations([{
      source_id: 'source-a',
      observation_id: 'det-1',
      layer_id: 'hidden-layer',
      feature_id: 'feature-99',
      overlay_resolution: 'matched',
      overlay_candidate_count: 1,
      dataset_position: [1010, 2000, 32],
      properties: { det_id: 'det-1', class_nm: 'traffic_sign' },
    }], [1000, 2000, 30], [])

    expect(points[0]).toMatchObject({
      layerId: 'hidden-layer',
      featureId: 'feature-99',
      selected: false,
    })
  })

  it('does not override an authoritative not-found result with a visible heuristic match', () => {
    const representative: RenderOverlayPoint = {
      layerId: 'layer-1',
      layerName: 'Detected signs',
      featureId: 'feature-7',
      color: '#22c55e',
      position: [10, 0, 2],
      selected: false,
      properties: { det_id: 'det-1' },
    }
    const points = pointCloudDetectionsFromObservations([{
      source_id: 'source-a',
      observation_id: 'det-1',
      overlay_resolution: 'not_found',
      overlay_candidate_count: 0,
      dataset_position: [1010, 2000, 32],
      properties: { det_id: 'det-1' },
    }], [1000, 2000, 30], [representative])

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
    expect(Math.min(...positions.slice(0, 12 * 2 * 3))).toBeCloseTo(9.78)
    expect(Math.max(...positions.slice(0, 12 * 2 * 3))).toBeCloseTo(30.22)
    expect(Math.min(...positions.slice(12 * 2 * 3))).toBeCloseTo(-0.28)
    expect(Math.max(...positions.slice(12 * 2 * 3))).toBeCloseTo(0.28)
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

  it('cycles through overlapping point hits on repeated clicks', () => {
    const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100)
    camera.position.set(0, 0, 0)
    camera.lookAt(0, 0, -1)
    camera.updateProjectionMatrix()
    camera.updateMatrixWorld(true)
    const indices = pointHitIndices(
      [
        { index: 3, point: new THREE.Vector3(0, 0, -5), distance: 5 },
        { index: 9, point: new THREE.Vector3(0, 0, -10), distance: 10 },
        // Duplicate Three intersections for one vertex must not create an
        // extra stop in the click cycle.
        { index: 3, point: new THREE.Vector3(0, 0, -5), distance: 5.1 },
      ],
      { x: 0, y: 0 },
      camera,
      800,
      800,
      7,
    )

    expect(indices).toEqual([3, 9])
    expect(nextCycledPointHit(indices, null)).toBe(3)
    expect(nextCycledPointHit(indices, 3)).toBe(9)
    expect(nextCycledPointHit(indices, 9)).toBe(3)
    expect(nextCycledPointHit(indices, 404)).toBe(3)
    expect(nextCycledPointHit([], null)).toBeNull()
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

  it('builds seed, axis, base, and guide previews in frame-local coordinates', () => {
    const result: PoleBaseInferResponse = {
      status: 'review',
      algorithm: 'manual_seed_axis_ground_intersection',
      algorithm_version: '1',
      coordinate_space: 'dataset',
      seed_position: [1002.05, 2003.05, 35],
      snapped_seed_position: [1002, 2003, 35],
      base_position: [1002.1, 2003.1, 30],
      axis: {
        point: [1002, 2003, 33],
        direction: [0, 0, 1],
        point_count: 120,
        observed_z_min: 31,
        observed_z_max: 37,
        vertical_span_m: 6,
        vertical_bin_count: 24,
        longest_consecutive_bin_count: 22,
        occupancy_ratio: 0.9,
        rmse_m: 0.04,
        tilt_deg: 0,
        seed_distance_m: 0.05,
      },
      ground: {
        method: 'plane',
        z_at_base: 30,
        rmse_m: 0.05,
        cell_count: 12,
        candidate_cell_count: 15,
        nearest_support_distance_m: 0.2,
        plane_coefficients: [0, 0, 30],
        reference_xy: [1002.1, 2003.1],
      },
      quality: {
        score: 0.84,
        candidate_count: 1,
        ambiguous: false,
        bottom_gap_m: 1,
        components: {
          seed: 1,
          axis: 1,
          span: 1,
          continuity: 0.9,
          ground: 0.9,
          bottom_gap: 0.7,
        },
      },
      reason_codes: ['BOTTOM_EXTRAPOLATED'],
      warnings: ['Bottom extrapolation is longer than the automatic gate.'],
    }

    const preview = poleBasePreviewGeometry(
      result.seed_position,
      result,
      [1000, 2000, 30],
    )
    expect(preview.seed[0]).toBeCloseTo(2.05)
    expect(preview.seed[1]).toBeCloseTo(3.05)
    expect(preview.seed[2]).toBe(5)
    expect(preview.base?.[0]).toBeCloseTo(2.1)
    expect(preview.base?.[1]).toBeCloseTo(3.1)
    expect(preview.base?.[2]).toBe(0)
    expect(preview.axis).toEqual([[2, 3, 1], [2, 3, 7]])
    expect(preview.guide?.[0]).toEqual(preview.seed)
    expect(preview.guide?.[1]).toEqual(preview.base)
    expect(poleBaseStatusLabel(result.status)).toBe('검토 필요')
    expect(poleBasePrimaryWarning(result)).toBe(
      '관측된 지주 끝에서 바닥까지 외삽 거리가 깁니다.',
    )
  })

  it('keeps a seed-only preview while inference is loading', () => {
    expect(poleBasePreviewGeometry([11, 22, 33], null, [10, 20, 30])).toEqual({
      seed: [1, 2, 3],
      base: null,
      axis: null,
      guide: null,
    })
  })

  it('routes a pole target to inference with the unchanged dataset XYZ', async () => {
    const actions = {
      applyPointCloudCoordinate: vi.fn().mockResolvedValue(undefined),
      applyPoleSeed: vi.fn().mockResolvedValue(undefined),
    }
    const datasetCoordinates: [number, number, number] = [209123.456, 412345.678, 35.912]

    await applyPointCloudPickedCoordinate(
      { kind: 'pole-base-create', layerId: 'poles', continuous: true },
      'frame-17',
      datasetCoordinates,
      actions,
    )

    expect(actions.applyPoleSeed).toHaveBeenCalledWith('frame-17', datasetCoordinates)
    expect(actions.applyPointCloudCoordinate).not.toHaveBeenCalled()

    await applyPointCloudPickedCoordinate(
      { kind: 'move', layerId: 'poles', featureId: 'pole-1' },
      'frame-17',
      datasetCoordinates,
      actions,
    )
    expect(actions.applyPointCloudCoordinate).toHaveBeenCalledWith('frame-17', datasetCoordinates)
  })

  it('accepts pole clicks only while the proposal is picking', () => {
    const target = { kind: 'pole-base-move', layerId: 'poles', featureId: 'pole-1' } as const
    expect(pointCloudPickTargetAcceptsPoint(target, 'picking')).toBe(true)
    expect(pointCloudPickTargetAcceptsPoint(target, 'loading')).toBe(false)
    expect(pointCloudPickTargetAcceptsPoint(target, 'ready')).toBe(false)
    expect(pointCloudPickTargetAcceptsPoint(target, 'error')).toBe(false)
    expect(
      pointCloudPickTargetAcceptsPoint(
        { kind: 'move', layerId: 'poles', featureId: 'pole-1' },
        'ready',
      ),
    ).toBe(true)
  })
})
