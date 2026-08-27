import * as THREE from 'three'

const MIN_ACCELERATED_POINT_COUNT = 50_000
const TARGET_POINTS_PER_CELL = 96
const MAX_GRID_CELLS_PER_AXIS = 256

interface PointGridIndex {
  attribute: THREE.BufferAttribute | THREE.InterleavedBufferAttribute
  attributeVersion: number
  pointCount: number
  bounds: THREE.Box3
  minX: number
  minY: number
  maxX: number
  maxY: number
  cellSize: number
  cellsX: number
  cellsY: number
  heads: Int32Array
  next: Int32Array
  occupiedCellCount: number
  visitMarks: Uint32Array
  visitGeneration: number
  buildCount: number
  lastCandidateCount: number
  lastVisitedCellCount: number
}

export interface PointRaycastIndexStats {
  pointCount: number
  cellCount: number
  cellSize: number
  buildCount: number
  lastCandidateCount: number
  lastVisitedCellCount: number
}

const pointGridCache = new WeakMap<THREE.BufferGeometry, PointGridIndex>()
const originalPointsRaycast = THREE.Points.prototype.raycast
let installed = false

const inverseMatrix = new THREE.Matrix4()
const localRay = new THREE.Ray()
const worldSphere = new THREE.Sphere()
const localBounds = new THREE.Box3()
const point = new THREE.Vector3()
const closestPoint = new THREE.Vector3()
const worldPoint = new THREE.Vector3()
const scale = new THREE.Vector3()
const segmentStart = new THREE.Vector3()
const segmentEnd = new THREE.Vector3()

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}

function finiteBounds(bounds: THREE.Box3): boolean {
  return [
    bounds.min.x,
    bounds.min.y,
    bounds.min.z,
    bounds.max.x,
    bounds.max.y,
    bounds.max.z,
  ].every(Number.isFinite)
}

function attributeVersion(
  attribute: THREE.BufferAttribute | THREE.InterleavedBufferAttribute,
): number {
  return attribute instanceof THREE.BufferAttribute
    ? attribute.version
    : attribute.data.version
}

function pointCoordinate(
  attribute: THREE.BufferAttribute | THREE.InterleavedBufferAttribute,
  index: number,
  axis: 0 | 1,
): number {
  if (attribute instanceof THREE.BufferAttribute && !attribute.normalized) {
    const offset = index * attribute.itemSize + axis
    return Number((attribute.array as ArrayLike<number>)[offset])
  }
  return axis === 0 ? attribute.getX(index) : attribute.getY(index)
}

function chooseCellSize(bounds: THREE.Box3, pointCount: number): number {
  const spanX = Math.max(0, bounds.max.x - bounds.min.x)
  const spanY = Math.max(0, bounds.max.y - bounds.min.y)
  const maximumSpan = Math.max(spanX, spanY)
  if (!Number.isFinite(maximumSpan) || maximumSpan <= Number.EPSILON) return 1

  // Use the square of the larger span as a conservative fallback for narrow
  // strips. This prevents a nearly one-dimensional delivery from creating
  // thousands of tiny cells with no useful pruning.
  const effectiveArea = Math.max(spanX * spanY, maximumSpan * maximumSpan * 0.0625)
  const ideal = Math.sqrt(
    effectiveArea * TARGET_POINTS_PER_CELL / Math.max(1, pointCount),
  )
  const minimum = maximumSpan / MAX_GRID_CELLS_PER_AXIS
  const maximum = Math.max(minimum, maximumSpan / 8)
  return clamp(ideal, Math.max(minimum, Number.EPSILON), maximum)
}

function buildPointGrid(
  geometry: THREE.BufferGeometry,
  attribute: THREE.BufferAttribute | THREE.InterleavedBufferAttribute,
  previousBuildCount = 0,
): PointGridIndex | null {
  geometry.computeBoundingBox()
  const bounds = geometry.boundingBox?.clone()
  if (!bounds || bounds.isEmpty() || !finiteBounds(bounds)) return null

  const pointCount = attribute.count
  const cellSize = chooseCellSize(bounds, pointCount)
  const spanX = Math.max(0, bounds.max.x - bounds.min.x)
  const spanY = Math.max(0, bounds.max.y - bounds.min.y)
  const cellsX = Math.max(1, Math.ceil(spanX / cellSize))
  const cellsY = Math.max(1, Math.ceil(spanY / cellSize))
  const totalCells = cellsX * cellsY
  const heads = new Int32Array(totalCells)
  heads.fill(-1)
  const next = new Int32Array(pointCount)
  next.fill(-1)
  let occupiedCellCount = 0

  for (let index = 0; index < pointCount; index += 1) {
    const x = pointCoordinate(attribute, index, 0)
    const y = pointCoordinate(attribute, index, 1)
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    const cellX = clamp(Math.floor((x - bounds.min.x) / cellSize), 0, cellsX - 1)
    const cellY = clamp(Math.floor((y - bounds.min.y) / cellSize), 0, cellsY - 1)
    const cellId = cellY * cellsX + cellX
    if (heads[cellId] < 0) occupiedCellCount += 1
    next[index] = heads[cellId]
    heads[cellId] = index
  }

  return {
    attribute,
    attributeVersion: attributeVersion(attribute),
    pointCount,
    bounds,
    minX: bounds.min.x,
    minY: bounds.min.y,
    maxX: bounds.max.x,
    maxY: bounds.max.y,
    cellSize,
    cellsX,
    cellsY,
    heads,
    next,
    occupiedCellCount,
    visitMarks: new Uint32Array(totalCells),
    visitGeneration: 0,
    buildCount: previousBuildCount + 1,
    lastCandidateCount: 0,
    lastVisitedCellCount: 0,
  }
}

function pointGrid(
  geometry: THREE.BufferGeometry,
  attribute: THREE.BufferAttribute | THREE.InterleavedBufferAttribute,
): PointGridIndex | null {
  const cached = pointGridCache.get(geometry)
  if (
    cached &&
    cached.attribute === attribute &&
    cached.attributeVersion === attributeVersion(attribute) &&
    cached.pointCount === attribute.count
  ) return cached

  const built = buildPointGrid(geometry, attribute, cached?.buildCount ?? 0)
  if (built) pointGridCache.set(geometry, built)
  return built
}

function rayBoxInterval(ray: THREE.Ray, bounds: THREE.Box3): [number, number] | null {
  let enter = 0
  let leave = Number.POSITIVE_INFINITY
  const origins = [ray.origin.x, ray.origin.y, ray.origin.z]
  const directions = [ray.direction.x, ray.direction.y, ray.direction.z]
  const minimums = [bounds.min.x, bounds.min.y, bounds.min.z]
  const maximums = [bounds.max.x, bounds.max.y, bounds.max.z]

  for (let axis = 0; axis < 3; axis += 1) {
    const origin = origins[axis]
    const direction = directions[axis]
    if (Math.abs(direction) <= 1e-12) {
      if (origin < minimums[axis] || origin > maximums[axis]) return null
      continue
    }
    let first = (minimums[axis] - origin) / direction
    let second = (maximums[axis] - origin) / direction
    if (first > second) [first, second] = [second, first]
    enter = Math.max(enter, first)
    leave = Math.min(leave, second)
    if (enter > leave) return null
  }
  return Number.isFinite(leave) ? [enter, leave] : null
}

function clampedGridCoordinate(
  value: number,
  minimum: number,
  maximum: number,
): number {
  if (maximum <= minimum) return minimum
  // Keep an exact maximum inside the final cell rather than producing the
  // first coordinate beyond the grid.
  return clamp(value, minimum, maximum - Math.max(Number.EPSILON, (maximum - minimum) * 1e-12))
}

function visitRayCells(
  index: PointGridIndex,
  start: THREE.Vector3,
  end: THREE.Vector3,
  neighborCells: number,
  visit: (cellId: number) => void,
): number {
  const startX = clampedGridCoordinate(start.x, index.minX, index.maxX)
  const startY = clampedGridCoordinate(start.y, index.minY, index.maxY)
  const endX = clampedGridCoordinate(end.x, index.minX, index.maxX)
  const endY = clampedGridCoordinate(end.y, index.minY, index.maxY)
  const deltaX = endX - startX
  const deltaY = endY - startY
  let cellX = clamp(Math.floor((startX - index.minX) / index.cellSize), 0, index.cellsX - 1)
  let cellY = clamp(Math.floor((startY - index.minY) / index.cellSize), 0, index.cellsY - 1)
  const endCellX = clamp(Math.floor((endX - index.minX) / index.cellSize), 0, index.cellsX - 1)
  const endCellY = clamp(Math.floor((endY - index.minY) / index.cellSize), 0, index.cellsY - 1)
  const stepX = Math.sign(deltaX)
  const stepY = Math.sign(deltaY)
  const tDeltaX = stepX === 0 ? Number.POSITIVE_INFINITY : index.cellSize / Math.abs(deltaX)
  const tDeltaY = stepY === 0 ? Number.POSITIVE_INFINITY : index.cellSize / Math.abs(deltaY)
  const nextBoundaryX = index.minX + (stepX > 0 ? cellX + 1 : cellX) * index.cellSize
  const nextBoundaryY = index.minY + (stepY > 0 ? cellY + 1 : cellY) * index.cellSize
  let tMaxX = stepX === 0 ? Number.POSITIVE_INFINITY : (nextBoundaryX - startX) / deltaX
  let tMaxY = stepY === 0 ? Number.POSITIVE_INFINITY : (nextBoundaryY - startY) / deltaY
  index.visitGeneration = (index.visitGeneration + 1) >>> 0
  if (index.visitGeneration === 0) {
    index.visitMarks.fill(0)
    index.visitGeneration = 1
  }
  const visitGeneration = index.visitGeneration
  let visitedCellCount = 0
  const maximumSteps = index.cellsX + index.cellsY + 4

  for (let step = 0; step < maximumSteps; step += 1) {
    for (let offsetY = -neighborCells; offsetY <= neighborCells; offsetY += 1) {
      const neighborY = cellY + offsetY
      if (neighborY < 0 || neighborY >= index.cellsY) continue
      for (let offsetX = -neighborCells; offsetX <= neighborCells; offsetX += 1) {
        const neighborX = cellX + offsetX
        if (neighborX < 0 || neighborX >= index.cellsX) continue
        const cellId = neighborY * index.cellsX + neighborX
        if (index.visitMarks[cellId] === visitGeneration) continue
        index.visitMarks[cellId] = visitGeneration
        visitedCellCount += 1
        visit(cellId)
      }
    }
    if (cellX === endCellX && cellY === endCellY) break
    if (tMaxX < tMaxY) {
      cellX += stepX
      tMaxX += tDeltaX
    } else if (tMaxY < tMaxX) {
      cellY += stepY
      tMaxY += tDeltaY
    } else {
      cellX += stepX
      cellY += stepY
      tMaxX += tDeltaX
      tMaxY += tDeltaY
    }
    if (cellX < 0 || cellX >= index.cellsX || cellY < 0 || cellY >= index.cellsY) break
  }
  return visitedCellCount
}

function fallbackRaycast(
  object: THREE.Points,
  raycaster: THREE.Raycaster,
  intersects: THREE.Intersection[],
): void {
  originalPointsRaycast.call(object, raycaster, intersects)
}

/**
 * Raycast a large point set through a cached XY grid instead of visiting every
 * vertex. The final point-to-ray test is identical to Three.js, so the caller's
 * existing screen-space ranking and overlap cycling remain unchanged.
 */
export function acceleratedPointsRaycast(
  this: THREE.Points,
  raycaster: THREE.Raycaster,
  intersects: THREE.Intersection[],
): void {
  const geometry = this.geometry
  if (!(geometry instanceof THREE.BufferGeometry)) {
    fallbackRaycast(this, raycaster, intersects)
    return
  }
  const attribute = geometry.getAttribute('position')
  if (
    !attribute ||
    geometry.index !== null ||
    attribute.count < MIN_ACCELERATED_POINT_COUNT ||
    (geometry.morphAttributes.position?.length ?? 0) > 0
  ) {
    fallbackRaycast(this, raycaster, intersects)
    return
  }

  const threshold = raycaster.params.Points?.threshold ?? 1
  if (!Number.isFinite(threshold) || threshold <= 0) return
  if (geometry.boundingSphere === null) geometry.computeBoundingSphere()
  if (!geometry.boundingSphere) return
  worldSphere.copy(geometry.boundingSphere).applyMatrix4(this.matrixWorld)
  worldSphere.radius += threshold
  if (!raycaster.ray.intersectsSphere(worldSphere)) return

  inverseMatrix.copy(this.matrixWorld).invert()
  localRay.copy(raycaster.ray).applyMatrix4(inverseMatrix)
  scale.copy(this.scale)
  const averageScale = (scale.x + scale.y + scale.z) / 3
  if (!Number.isFinite(averageScale) || Math.abs(averageScale) <= Number.EPSILON) {
    fallbackRaycast(this, raycaster, intersects)
    return
  }
  const localThreshold = threshold / averageScale
  const localThresholdMagnitude = Math.abs(localThreshold)
  const localThresholdSquared = localThreshold * localThreshold
  const index = pointGrid(geometry, attribute)
  if (!index) {
    fallbackRaycast(this, raycaster, intersects)
    return
  }

  localBounds.copy(index.bounds).expandByScalar(localThresholdMagnitude)
  const interval = rayBoxInterval(localRay, localBounds)
  if (!interval) {
    index.lastCandidateCount = 0
    index.lastVisitedCellCount = 0
    return
  }
  localRay.at(interval[0], segmentStart)
  localRay.at(interval[1], segmentEnd)

  const drawStart = Math.max(0, geometry.drawRange.start)
  const drawCount = Number.isFinite(geometry.drawRange.count)
    ? Math.max(0, geometry.drawRange.count)
    : attribute.count
  const drawEnd = Math.min(attribute.count, drawStart + drawCount)
  const neighborCells = Math.max(1, Math.ceil(localThresholdMagnitude / index.cellSize))
  let candidateCount = 0
  const visitedCellCount = visitRayCells(
    index,
    segmentStart,
    segmentEnd,
    neighborCells,
    (cellId) => {
      let pointIndex = index.heads[cellId]
      while (pointIndex >= 0) {
        if (pointIndex >= drawStart && pointIndex < drawEnd) {
          candidateCount += 1
          point.fromBufferAttribute(attribute, pointIndex)
          const distanceSquared = localRay.distanceSqToPoint(point)
          if (distanceSquared < localThresholdSquared) {
            localRay.closestPointToPoint(point, closestPoint)
            worldPoint.copy(closestPoint).applyMatrix4(this.matrixWorld)
            const distance = raycaster.ray.origin.distanceTo(worldPoint)
            if (distance >= raycaster.near && distance <= raycaster.far) {
              intersects.push({
                distance,
                distanceToRay: Math.sqrt(distanceSquared),
                point: worldPoint.clone(),
                index: pointIndex,
                face: null,
                faceIndex: null,
                barycoord: null,
                object: this,
              })
            }
          }
        }
        pointIndex = index.next[pointIndex]
      }
    },
  )
  index.lastCandidateCount = candidateCount
  index.lastVisitedCellCount = visitedCellCount
}

/** Install once at application startup; small marker clouds keep native raycasting. */
export function installAcceleratedPointRaycast(): void {
  if (installed) return
  THREE.Points.prototype.raycast = acceleratedPointsRaycast
  installed = true
}

export function pointRaycastIndexStats(
  geometry: THREE.BufferGeometry,
): PointRaycastIndexStats | null {
  const index = pointGridCache.get(geometry)
  if (!index) return null
  return {
    pointCount: index.pointCount,
    cellCount: index.occupiedCellCount,
    cellSize: index.cellSize,
    buildCount: index.buildCount,
    lastCandidateCount: index.lastCandidateCount,
    lastVisitedCellCount: index.lastVisitedCellCount,
  }
}
