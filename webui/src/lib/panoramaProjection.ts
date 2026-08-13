import type { PanoramaProjectionMetadata } from '../types'

export interface PanoramaHoverProjection {
  frameId: string
  u: number
  v: number
  depth: number
}

type Vector3Tuple = readonly [number, number, number]

function dot(left: Vector3Tuple, right: Vector3Tuple): number {
  return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]
}

function finiteVector(value: readonly number[]): value is Vector3Tuple {
  return value.length === 3 && value.every(Number.isFinite)
}

export function projectFrameLocalPointToPanorama(
  localPoint: Vector3Tuple,
  metadata: PanoramaProjectionMetadata,
): Omit<PanoramaHoverProjection, 'frameId'> | null {
  if (
    !finiteVector(localPoint) ||
    !finiteVector(metadata.forward) ||
    !finiteVector(metadata.right) ||
    !finiteVector(metadata.up)
  ) {
    return null
  }
  const depth = Math.hypot(localPoint[0], localPoint[1], localPoint[2])
  if (!Number.isFinite(depth) || depth <= 0.05) return null
  const unit: Vector3Tuple = [
    localPoint[0] / depth,
    localPoint[1] / depth,
    localPoint[2] / depth,
  ]
  const localX = dot(unit, metadata.right)
  const localY = Math.min(1, Math.max(-1, dot(unit, metadata.up)))
  const localZ = dot(unit, metadata.forward)
  const rawU = Math.atan2(localX, localZ) / (2 * Math.PI) + 0.5
  return {
    u: ((rawU % 1) + 1) % 1,
    v: Math.min(1, Math.max(0, 0.5 - Math.asin(localY) / Math.PI)),
    depth,
  }
}

export function projectDatasetPointToPanorama(
  datasetPoint: Vector3Tuple,
  metadata: PanoramaProjectionMetadata,
): Omit<PanoramaHoverProjection, 'frameId'> | null {
  if (!finiteVector(datasetPoint) || !finiteVector(metadata.origin)) return null
  return projectFrameLocalPointToPanorama(
    [
      datasetPoint[0] - metadata.origin[0],
      datasetPoint[1] - metadata.origin[1],
      datasetPoint[2] - metadata.origin[2],
    ],
    metadata,
  )
}

export function panoramaUvToSpherePosition(
  u: number,
  v: number,
  radius = 9.88,
): [number, number, number] | null {
  if (![u, v, radius].every(Number.isFinite) || radius <= 0) return null
  const direction = panoramaUvToLocalDirection(u, v)
  if (!direction) return null
  const [localRight, localUp, localForward] = direction
  // PanoramaView's inward-facing SphereGeometry is mirrored on X. Its viewer
  // axes are therefore -X=forward, -Z=right and +Y=up. Keeping this mapping
  // explicit prevents a texture-UV convention from silently shifting linked
  // point markers when the sphere implementation changes.
  return [
    -radius * localForward,
    radius * localUp,
    -radius * localRight,
  ]
}

/** Convert top-left-origin equirectangular UV to [right, up, forward]. */
export function panoramaUvToLocalDirection(
  u: number,
  v: number,
): [number, number, number] | null {
  if (![u, v].every(Number.isFinite)) return null
  const wrappedU = ((u % 1) + 1) % 1
  const clampedV = Math.min(1, Math.max(0, v))
  const longitude = (wrappedU - 0.5) * Math.PI * 2
  const latitude = (0.5 - clampedV) * Math.PI
  const cosLatitude = Math.cos(latitude)
  return [
    cosLatitude * Math.sin(longitude),
    Math.sin(latitude),
    cosLatitude * Math.cos(longitude),
  ]
}
