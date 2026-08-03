import { Crosshair, Layers, LoaderCircle, Navigation2 } from 'lucide-react'
import type { FeatureCollection } from 'geojson'
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap, type StyleSpecification } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  buildRouteFeatureCollection,
  buildRouteRangeFeatureCollection,
  buildTrackColorMap,
  TRACK_COLORS,
} from '../lib/route'
import type { Frame, FrameRange, RoutePoint } from '../types'

interface MapViewProps {
  route: RoutePoint[]
  frames: Frame[]
  selectedFrame: Frame | null
  frameRange?: FrameRange | null
  loading: boolean
  mapStyleUrl?: string
  onSelectFrame: (frame: Frame) => void
}

const fallbackStyle: StyleSpecification = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      attribution: '© OpenStreetMap contributors',
      maxzoom: 19,
    },
  },
  layers: [
    {
      id: 'osm',
      type: 'raster',
      source: 'osm',
      paint: {
        'raster-saturation': -0.78,
        'raster-contrast': 0.08,
        'raster-brightness-min': 0.18,
        'raster-brightness-max': 0.8,
      },
    },
  ],
}

export function MapView({
  route,
  frames,
  selectedFrame,
  frameRange,
  loading,
  mapStyleUrl,
  onSelectFrame,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const onSelectRef = useRef(onSelectFrame)
  const framesRef = useRef(frames)
  const fittedRouteRef = useRef('')
  const selectedFrameRef = useRef<string | null>(null)
  const [ready, setReady] = useState(false)
  const [satellite, setSatellite] = useState(false)

  onSelectRef.current = onSelectFrame
  framesRef.current = frames

  const trackColors = useMemo(() => buildTrackColorMap(route), [route])
  const frameIndexes = useMemo(
    () => new Map(frames.map((frame) => [frame.id, frame.index])),
    [frames],
  )
  const frameGeoJson = useMemo<FeatureCollection>(
    () => ({
      type: 'FeatureCollection',
      features: frames
        .filter((frame) => frame.coordinate !== null)
        .map((frame) => ({
          type: 'Feature',
          id: frame.id,
          properties: {
            id: frame.id,
            selected: frame.id === selectedFrame?.id ? 1 : 0,
            in_range:
              frameRange && frame.index >= frameRange[0] && frame.index <= frameRange[1] ? 1 : 0,
            track_color: trackColors.get(frame.track_id) ?? TRACK_COLORS[0],
          },
          geometry: {
            type: 'Point',
            coordinates: [frame.coordinate!.lon, frame.coordinate!.lat],
          },
        })),
    }),
    [frameRange, frames, selectedFrame, trackColors],
  )

  const routeGeoJson = useMemo(() => buildRouteFeatureCollection(route), [route])
  const routeRangeGeoJson = useMemo(
    () => buildRouteRangeFeatureCollection(route, frameIndexes, frameRange),
    [frameIndexes, frameRange, route],
  )

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: mapStyleUrl ?? fallbackStyle,
      center: route[0] ? [route[0].lon, route[0].lat] : [126.978, 37.5665],
      zoom: route.length ? 13.8 : 11,
      pitch: 34,
      bearing: -14,
      attributionControl: false,
      maxPitch: 70,
    })
    mapRef.current = map
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'bottom-right')
    map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-left')

    const resizeObserver = new ResizeObserver(() => map.resize())
    resizeObserver.observe(containerRef.current)

    map.on('load', () => {
      map.addSource('mms-route', { type: 'geojson', data: routeGeoJson })
      map.addLayer({
        id: 'mms-route-halo',
        type: 'line',
        source: 'mms-route',
        paint: { 'line-color': '#07111f', 'line-width': 8, 'line-opacity': 0.55 },
      })
      map.addLayer({
        id: 'mms-route-line',
        type: 'line',
        source: 'mms-route',
        paint: {
          'line-color': ['coalesce', ['get', 'track_color'], TRACK_COLORS[0]],
          'line-width': 4,
          'line-opacity': 0.95,
        },
      })
      map.addSource('mms-route-range', { type: 'geojson', data: routeRangeGeoJson })
      map.addLayer({
        id: 'mms-route-range-halo',
        type: 'line',
        source: 'mms-route-range',
        paint: { 'line-color': '#ffffff', 'line-width': 10, 'line-opacity': 0.72 },
      })
      map.addLayer({
        id: 'mms-route-range-line',
        type: 'line',
        source: 'mms-route-range',
        paint: {
          'line-color': ['coalesce', ['get', 'track_color'], TRACK_COLORS[0]],
          'line-width': 6,
          'line-opacity': 1,
        },
      })
      map.addSource('mms-frames', { type: 'geojson', data: frameGeoJson })
      map.addLayer({
        id: 'mms-frames-points',
        type: 'circle',
        source: 'mms-frames',
        paint: {
          'circle-radius': ['case', ['==', ['get', 'selected'], 1], 8, 3],
          'circle-color': [
            'case',
            ['==', ['get', 'selected'], 1],
            '#ffffff',
            ['coalesce', ['get', 'track_color'], TRACK_COLORS[0]],
          ],
          'circle-stroke-width': [
            'case',
            ['==', ['get', 'selected'], 1],
            4,
            ['==', ['get', 'in_range'], 1],
            3,
            1,
          ],
          'circle-stroke-color': [
            'case',
            ['==', ['get', 'selected'], 1],
            ['coalesce', ['get', 'track_color'], TRACK_COLORS[0]],
            ['==', ['get', 'in_range'], 1],
            '#ffffff',
            '#09261f',
          ],
          'circle-opacity': ['case', ['==', ['get', 'in_range'], 1], 1, 0.82],
        },
      })
      map.on('mouseenter', 'mms-frames-points', () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', 'mms-frames-points', () => {
        map.getCanvas().style.cursor = ''
      })
      map.on('click', 'mms-frames-points', (event) => {
        const id = event.features?.[0]?.properties?.id as string | undefined
        const frame = framesRef.current.find((candidate) => candidate.id === id)
        if (frame) onSelectRef.current(frame)
      })
      setReady(true)
    })

    return () => {
      resizeObserver.disconnect()
      map.remove()
      mapRef.current = null
    }
    // The map instance owns its initial style; later data changes use sources below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapStyleUrl])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready) return
    ;(map.getSource('mms-route') as GeoJSONSource | undefined)?.setData(routeGeoJson)
    if (route.length > 1) {
      const routeKey = `${route[0]?.frame_id ?? ''}:${route.at(-1)?.frame_id ?? ''}:${route.length}`
      const bounds = route.reduce(
        (result, point) => result.extend([point.lon, point.lat]),
        new maplibregl.LngLatBounds([route[0].lon, route[0].lat], [route[0].lon, route[0].lat]),
      )
      map.fitBounds(bounds, {
        padding: 88,
        duration: fittedRouteRef.current === routeKey ? 450 : 0,
        maxZoom: 17,
      })
      fittedRouteRef.current = routeKey
    }
  }, [ready, route, routeGeoJson])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready) return
    ;(map.getSource('mms-route-range') as GeoJSONSource | undefined)?.setData(routeRangeGeoJson)
  }, [ready, routeRangeGeoJson])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !ready) return
    ;(map.getSource('mms-frames') as GeoJSONSource | undefined)?.setData(frameGeoJson)
  }, [frameGeoJson, ready])

  useEffect(() => {
    const map = mapRef.current
    if (!selectedFrame?.coordinate) {
      selectedFrameRef.current = null
      return
    }
    if (selectedFrameRef.current === null) {
      selectedFrameRef.current = selectedFrame.id
      return
    }
    if (!map || !ready || selectedFrameRef.current === selectedFrame.id) return
    selectedFrameRef.current = selectedFrame.id
    map.easeTo({
      center: [selectedFrame.coordinate.lon, selectedFrame.coordinate.lat],
      duration: 520,
      zoom: Math.max(map.getZoom(), 16),
    })
  }, [ready, selectedFrame])

  const recenter = () => {
    if (!selectedFrame?.coordinate || !mapRef.current) return
    mapRef.current.flyTo({
      center: [selectedFrame.coordinate.lon, selectedFrame.coordinate.lat],
      zoom: 17.2,
      pitch: 46,
      bearing: -(selectedFrame.heading ?? 0),
      duration: 900,
    })
  }

  return (
    <div className={`map-view ${satellite ? 'satellite-mode' : ''}`}>
      <div ref={containerRef} className="map-container" />
      {loading && (
        <div className="map-loading">
          <LoaderCircle size={15} className="spin" />
          이동 경로 불러오는 중
        </div>
      )}
      <div className="map-tools">
        <button type="button" onClick={recenter} disabled={!selectedFrame?.coordinate}>
          <Crosshair size={16} />
          현재 프레임
        </button>
        <button type="button" onClick={() => setSatellite((value) => !value)}>
          <Layers size={16} />
          {satellite ? '기본 톤' : '고대비'}
        </button>
      </div>
      <div className="map-legend">
        <span>
          <i className="legend-route" />
          주행 경로
        </span>
        <span>
          <i className="legend-frame" />
          MMS 프레임
        </span>
        {selectedFrame && (
          <span className="map-bearing">
            <Navigation2 size={13} style={{ rotate: `${selectedFrame.heading ?? 0}deg` }} />
            {Math.round(selectedFrame.heading ?? 0)}°
          </span>
        )}
      </div>
    </div>
  )
}
