import { describe, expect, it } from 'vitest'
import * as THREE from 'three'
import {
  acceleratedPointsRaycast,
  pointRaycastIndexStats,
} from './acceleratedPointRaycast'

function denseGrid(columns = 300, rows = 200): THREE.Points {
  const positions = new Float32Array(columns * rows * 3)
  let offset = 0
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      positions[offset] = (column - columns / 2) * 0.025
      positions[offset + 1] = (row - rows / 2) * 0.025
      positions[offset + 2] = ((column + row) % 7) * 0.002
      offset += 3
    }
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  const points = new THREE.Points(geometry, new THREE.PointsMaterial())
  points.updateMatrixWorld(true)
  return points
}

function hitIndices(intersections: THREE.Intersection[]): number[] {
  return intersections
    .map((entry) => entry.index)
    .filter((index): index is number => index !== undefined)
    .sort((left, right) => left - right)
}

function deterministicRandom(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0
    return state / 0x1_0000_0000
  }
}

describe('accelerated point raycast', () => {
  it('returns the same point indices as the native Three.js raycast', () => {
    const points = denseGrid()
    const raycaster = new THREE.Raycaster(
      new THREE.Vector3(0.011, -0.007, 5),
      new THREE.Vector3(0.04, 0.02, -1).normalize(),
      0,
      20,
    )
    raycaster.params.Points = { threshold: 0.035 }
    const nativeIntersections: THREE.Intersection[] = []
    THREE.Points.prototype.raycast.call(points, raycaster, nativeIntersections)
    const acceleratedIntersections: THREE.Intersection[] = []
    acceleratedPointsRaycast.call(points, raycaster, acceleratedIntersections)

    expect(hitIndices(acceleratedIntersections)).toEqual(hitIndices(nativeIntersections))
    const stats = pointRaycastIndexStats(points.geometry)
    expect(stats).not.toBeNull()
    expect(stats!.lastCandidateCount).toBeLessThan(stats!.pointCount / 8)
  })

  it('keeps small marker clouds on the native raycast path', () => {
    const points = denseGrid(100, 100)
    const raycaster = new THREE.Raycaster(
      new THREE.Vector3(0, 0, 4),
      new THREE.Vector3(0, 0, -1),
      0,
      10,
    )
    raycaster.params.Points = { threshold: 0.04 }
    const nativeIntersections: THREE.Intersection[] = []
    THREE.Points.prototype.raycast.call(points, raycaster, nativeIntersections)
    const acceleratedIntersections: THREE.Intersection[] = []
    acceleratedPointsRaycast.call(points, raycaster, acceleratedIntersections)

    expect(hitIndices(acceleratedIntersections)).toEqual(hitIndices(nativeIntersections))
    expect(pointRaycastIndexStats(points.geometry)).toBeNull()
  })

  it('keeps edge points discoverable when the ray passes just outside the XY bounds', () => {
    const points = denseGrid()
    points.geometry.computeBoundingBox()
    const maximumX = points.geometry.boundingBox!.max.x
    const raycaster = new THREE.Raycaster(
      new THREE.Vector3(maximumX + 0.02, 0, 4),
      new THREE.Vector3(0, 0, -1),
      0,
      10,
    )
    raycaster.params.Points = { threshold: 0.04 }
    const nativeIntersections: THREE.Intersection[] = []
    THREE.Points.prototype.raycast.call(points, raycaster, nativeIntersections)
    const acceleratedIntersections: THREE.Intersection[] = []
    acceleratedPointsRaycast.call(points, raycaster, acceleratedIntersections)

    expect(nativeIntersections.length).toBeGreaterThan(0)
    expect(hitIndices(acceleratedIntersections)).toEqual(hitIndices(nativeIntersections))
  })

  it('rebuilds the cached grid when position data changes', () => {
    const points = denseGrid()
    const raycaster = new THREE.Raycaster(
      new THREE.Vector3(0, 0, 4),
      new THREE.Vector3(0, 0, -1),
      0,
      10,
    )
    raycaster.params.Points = { threshold: 0.04 }
    acceleratedPointsRaycast.call(points, raycaster, [])
    const first = pointRaycastIndexStats(points.geometry)

    const positions = points.geometry.getAttribute('position') as THREE.BufferAttribute
    positions.setX(0, positions.getX(0) + 0.5)
    positions.needsUpdate = true
    acceleratedPointsRaycast.call(points, raycaster, [])
    const second = pointRaycastIndexStats(points.geometry)

    expect(first?.buildCount).toBe(1)
    expect(second?.buildCount).toBe(2)
  })

  it('matches native hits across transforms, draw ranges, and varied rays', () => {
    const random = deterministicRandom(0x5eed1234)
    const pointCount = 60_000
    const positions = new Float32Array(pointCount * 3)
    for (let index = 0; index < pointCount; index += 1) {
      positions[index * 3] = (random() - 0.5) * 40
      positions[index * 3 + 1] = (random() - 0.5) * 24
      positions[index * 3 + 2] = (random() - 0.5) * 8
    }
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geometry.setDrawRange(137, pointCount - 911)
    const points = new THREE.Points(geometry, new THREE.PointsMaterial())
    points.position.set(13, -7, 4)
    points.rotation.set(0.31, -0.23, 0.47)
    points.scale.set(1.3, 0.85, 1.1)
    points.updateMatrixWorld(true)

    for (let iteration = 0; iteration < 40; iteration += 1) {
      const target = new THREE.Vector3(
        (random() - 0.5) * 30,
        (random() - 0.5) * 18,
        (random() - 0.5) * 6,
      ).applyMatrix4(points.matrixWorld)
      const origin = target.clone().add(new THREE.Vector3(
        (random() - 0.5) * 18,
        (random() - 0.5) * 18,
        12 + random() * 20,
      ))
      const raycaster = new THREE.Raycaster(
        origin,
        target.clone().sub(origin).normalize(),
        0.2,
        80,
      )
      raycaster.params.Points = { threshold: 0.08 + random() * 0.35 }
      const nativeIntersections: THREE.Intersection[] = []
      THREE.Points.prototype.raycast.call(points, raycaster, nativeIntersections)
      const acceleratedIntersections: THREE.Intersection[] = []
      acceleratedPointsRaycast.call(points, raycaster, acceleratedIntersections)

      expect(hitIndices(acceleratedIntersections), `ray ${iteration}`).toEqual(
        hitIndices(nativeIntersections),
      )
    }
  })
})
