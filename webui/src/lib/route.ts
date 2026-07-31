import type { FeatureCollection, LineString } from 'geojson'
import type { RoutePoint } from '../types'

export function buildRouteFeatureCollection(route: RoutePoint[]): FeatureCollection<LineString> {
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

  return {
    type: 'FeatureCollection',
    features: [...tracks.entries()]
      .filter(([, points]) => points.length > 1)
      .map(([trackId, points]) => ({
        type: 'Feature',
        properties: { track_id: trackId },
        geometry: {
          type: 'LineString',
          coordinates: points.map((point) => [point.lon, point.lat]),
        },
      })),
  }
}
