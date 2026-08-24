import type { FeatureCollection, LineString } from 'geojson'
import type { RoutePoint } from '../types'

export const TRACK_COLORS = [
  '#579cf2',
  '#ffb36b',
  '#a080e8',
  '#e67da2',
  '#39c6d8',
  '#e1c84b',
  '#63c779',
  '#f06c62',
  '#4ed4a8',
  '#cf7be8',
  '#8dbf4f',
  '#e99545',
] as const

export interface RouteFeatureProperties {
  track_id: string
  track_index: number
  track_color: string
  selected?: 0 | 1
}

function groupRouteByTrack(route: RoutePoint[]): Map<string, RoutePoint[]> {
  const tracks = new Map<string, RoutePoint[]>()
  route.forEach((point) => {
    const key = point.track_id ?? '__unassigned__'
    const points = tracks.get(key)
    if (points) {
      points.push(point)
    } else {
      tracks.set(key, [point])
    }
  })
  return tracks
}

export function buildTrackColorMap(
  route: RoutePoint[],
  preferredTrackIds: readonly string[] = [],
): Map<string, string> {
  const routeTrackIds = [...groupRouteByTrack(route).keys()]
  const knownTrackIds = new Set<string>()
  const orderedTrackIds = [...preferredTrackIds, ...routeTrackIds].filter((trackId) => {
    if (knownTrackIds.has(trackId)) return false
    knownTrackIds.add(trackId)
    return true
  })
  return new Map(
    orderedTrackIds.map((trackId, index) => [
      trackId,
      TRACK_COLORS[index % TRACK_COLORS.length],
    ]),
  )
}

export function buildRouteFeatureCollection(
  route: RoutePoint[],
  trackColors?: ReadonlyMap<string, string>,
  activeTrackId?: string,
): FeatureCollection<LineString, RouteFeatureProperties> {
  const tracks = groupRouteByTrack(route)

  return {
    type: 'FeatureCollection',
    features: [...tracks.entries()].flatMap(([trackId, points], trackIndex) =>
      points.length > 1
        ? [
            {
              type: 'Feature' as const,
              properties: {
                track_id: trackId,
                track_index: trackIndex,
                track_color:
                  trackColors?.get(trackId) ?? TRACK_COLORS[trackIndex % TRACK_COLORS.length],
                selected: activeTrackId && trackId === activeTrackId ? 1 : 0,
              },
              geometry: {
                type: 'LineString' as const,
                coordinates: points.map((point) => [point.lon, point.lat]),
              },
            },
          ]
        : [],
    ),
  }
}

/**
 * Builds only the contiguous pieces of the sampled route that fall inside an
 * execution range. `frameIndexes` intentionally comes from the loaded frame
 * catalogue because route samples carry stable frame ids, not UI ordinals.
 */
export function buildRouteRangeFeatureCollection(
  route: RoutePoint[],
  frameIndexes: ReadonlyMap<string, number>,
  frameRange: readonly [number, number] | null | undefined,
  trackColors?: ReadonlyMap<string, string>,
  activeTrackId?: string,
): FeatureCollection<LineString, RouteFeatureProperties> {
  if (!frameRange) return { type: 'FeatureCollection', features: [] }

  const [rangeStart, rangeEnd] = frameRange
  const tracks = groupRouteByTrack(route)
  const features: FeatureCollection<LineString, RouteFeatureProperties>['features'] = []

  ;[...tracks.entries()].forEach(([trackId, points], trackIndex) => {
    let segment: RoutePoint[] = []
    const flush = () => {
      if (segment.length > 1) {
        features.push({
          type: 'Feature',
          properties: {
            track_id: trackId,
            track_index: trackIndex,
            track_color:
              trackColors?.get(trackId) ?? TRACK_COLORS[trackIndex % TRACK_COLORS.length],
            selected: activeTrackId && trackId === activeTrackId ? 1 : 0,
          },
          geometry: {
            type: 'LineString',
            coordinates: segment.map((point) => [point.lon, point.lat]),
          },
        })
      }
      segment = []
    }

    points.forEach((point) => {
      const frameIndex = point.index ?? (point.frame_id ? frameIndexes.get(point.frame_id) : undefined)
      if (frameIndex !== undefined && frameIndex >= rangeStart && frameIndex <= rangeEnd) {
        segment.push(point)
      } else {
        flush()
      }
    })
    flush()
  })

  return { type: 'FeatureCollection', features }
}
