import type { Frame } from '../types'

export interface DirectionalPanoramaTarget {
  frame: Frame
  bearing: number | null
  angularDifference: number
  direction: -1 | 1
}

export function normalizePanoramaBearing(value: number): number {
  if (!Number.isFinite(value)) return 0
  return ((value % 360) + 360) % 360
}

export function signedPanoramaAngle(value: number): number {
  const normalized = normalizePanoramaBearing(value)
  return normalized > 180 ? normalized - 360 : normalized
}

export function panoramaViewRelativeBearing(yaw: number, forwardYaw: number): number {
  return signedPanoramaAngle(yaw - forwardYaw)
}

export function panoramaViewWorldBearing(
  frameHeading: number,
  yaw: number,
  forwardYaw: number,
): number {
  return normalizePanoramaBearing(frameHeading + panoramaViewRelativeBearing(yaw, forwardYaw))
}

export function coordinateBearing(from: Frame, to: Frame): number | null {
  if (!from.coordinate || !to.coordinate) return null
  const { lat: fromLat, lon: fromLon } = from.coordinate
  const { lat: toLat, lon: toLon } = to.coordinate
  if (![fromLat, fromLon, toLat, toLon].every(Number.isFinite)) return null
  if (fromLat === toLat && fromLon === toLon) return null

  const latitude1 = fromLat * Math.PI / 180
  const latitude2 = toLat * Math.PI / 180
  const longitudeDelta = (toLon - fromLon) * Math.PI / 180
  const y = Math.sin(longitudeDelta) * Math.cos(latitude2)
  const x = Math.cos(latitude1) * Math.sin(latitude2)
    - Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(longitudeDelta)
  return normalizePanoramaBearing(Math.atan2(y, x) * 180 / Math.PI)
}

function nearestTrackNeighbors(current: Frame, frames: Frame[]): Array<{
  frame: Frame
  direction: -1 | 1
}> {
  let previous: Frame | null = null
  let next: Frame | null = null
  frames.forEach((candidate) => {
    if (candidate.id === current.id || candidate.track_id !== current.track_id) return
    if (candidate.index < current.index && (!previous || candidate.index > previous.index)) {
      previous = candidate
    }
    if (candidate.index > current.index && (!next || candidate.index < next.index)) {
      next = candidate
    }
  })
  return [
    ...(previous ? [{ frame: previous, direction: -1 as const }] : []),
    ...(next ? [{ frame: next, direction: 1 as const }] : []),
  ]
}

/**
 * Select the adjacent frame that lies closest to the operator's current view.
 *
 * Only the immediately adjacent frame on either side of the current track is
 * considered. This avoids jumping to a distant frame when an MMS route crosses
 * itself. If coordinates or heading are unavailable, the image-space forward
 * hemisphere still provides a deterministic previous/next fallback.
 */
export function directionalPanoramaTarget(
  current: Frame,
  frames: Frame[],
  yaw: number,
  forwardYaw: number,
): DirectionalPanoramaTarget | null {
  const candidates = nearestTrackNeighbors(current, frames)
  if (!candidates.length) return null

  const relativeBearing = panoramaViewRelativeBearing(yaw, forwardYaw)
  const heading = typeof current.heading === 'number' && Number.isFinite(current.heading)
    ? current.heading
    : null
  const fallbackForwardBearing = candidates
    .filter((candidate) => candidate.direction === 1)
    .map((candidate) => coordinateBearing(current, candidate.frame))
    .find((bearing): bearing is number => bearing !== null)
  const baseHeading = heading ?? fallbackForwardBearing

  if (baseHeading === undefined) {
    const preferredDirection: -1 | 1 = Math.cos(relativeBearing * Math.PI / 180) >= 0 ? 1 : -1
    const preferred = candidates.find((candidate) => candidate.direction === preferredDirection)
      ?? candidates[0]
    return {
      ...preferred,
      bearing: null,
      angularDifference: preferred.direction === preferredDirection ? 0 : 180,
    }
  }

  const viewingBearing = panoramaViewWorldBearing(baseHeading, yaw, forwardYaw)
  const scored = candidates.map((candidate) => {
    const bearing = coordinateBearing(current, candidate.frame)
      ?? normalizePanoramaBearing(baseHeading + (candidate.direction === 1 ? 0 : 180))
    return {
      ...candidate,
      bearing,
      angularDifference: Math.abs(signedPanoramaAngle(bearing - viewingBearing)),
    }
  })
  scored.sort((left, right) => (
    left.angularDifference - right.angularDifference
    || right.direction - left.direction
  ))
  // A single loaded neighbor may sit behind the current view while the
  // forward page has not been fetched yet. Do not mislabel that opposite
  // frame as the view-direction target; the caller can use its paginated
  // previous/next callback instead.
  return scored[0].angularDifference <= 100 ? scored[0] : null
}
