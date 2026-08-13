import { describe, expect, it } from 'vitest'
import * as THREE from 'three'
import {
  capturePointCloudViewState,
  captureHeadingDirection,
  closestPointHitIndex,
  datasetPointToFrameLocal,
  demoPanoramaProjectionMetadata,
  pointCloudDetectionPointSize,
  restorePointCloudViewState,
} from './PointCloudView'

describe('PointCloudView camera continuity', () => {
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
