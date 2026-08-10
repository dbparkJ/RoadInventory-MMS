import { AlertTriangle, Crosshair, Layers, LoaderCircle, Navigation2, RotateCcw } from 'lucide-react'
import type { FeatureCollection } from 'geojson'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import { resolveMapTrackScope } from '../lib/mapScope'
import {
  buildRouteFeatureCollection,
  buildRouteRangeFeatureCollection,
  buildTrackColorMap,
  TRACK_COLORS,
} from '../lib/route'
import {
  cameraTargetForCoordinates,
  createVWorldDataSource,
  moveVWorldMap,
  pickedEntityId,
  removeVWorldDataSource,
  renderVWorldFrames,
  renderVWorldOverlay,
  renderVWorldRoute,
  renderVWorldRouteRange,
  resizeVWorldMap,
  startVWorldMap,
  VWORLD_CONTAINER_ID,
  VWORLD_IFRAME_URL,
  type VWorldCustomDataSource,
  type VWorldRuntime,
} from '../lib/vworld'
import type { Frame, FrameRange, RoutePoint } from '../types'

interface MapViewProps {
  route: RoutePoint[]
  frames: Frame[]
  selectedFrame: Frame | null
  activeTrackId?: string
  showAllTracks?: boolean
  frameRange?: FrameRange | null
  loading: boolean
  onSelectFrame: (frame: Frame) => void
}

interface VWorldBoot {
  frameWindow: Window
  promise: Promise<VWorldRuntime>
}

interface VWorldSources {
  route: VWorldCustomDataSource
  routeRange: VWorldCustomDataSource
  frames: VWorldCustomDataSource
  overlay: VWorldCustomDataSource
}

async function createVWorldSources(runtime: VWorldRuntime): Promise<VWorldSources> {
  const created: VWorldCustomDataSource[] = []
  const create = async (name: string) => {
    const source = await createVWorldDataSource(runtime, name)
    created.push(source)
    return source
  }
  try {
    return {
      route: await create('mms-route'),
      routeRange: await create('mms-route-range'),
      frames: await create('mms-frames'),
      overlay: await create('mms-overlay'),
    }
  } catch (reason) {
    created.forEach((source) => removeVWorldDataSource(runtime, source))
    throw reason
  }
}

export function MapView({
  route,
  frames,
  selectedFrame,
  activeTrackId,
  showAllTracks = false,
  frameRange,
  loading,
  onSelectFrame,
}: MapViewProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const bootRef = useRef<VWorldBoot | null>(null)
  const runtimeRef = useRef<VWorldRuntime | null>(null)
  const dataSourcesRef = useRef<VWorldSources | null>(null)
  const frameClickTargetsRef = useRef<ReadonlyMap<string, () => void>>(new Map())
  const overlayClickTargetsRef = useRef<ReadonlyMap<string, () => void>>(new Map())
  const onSelectRef = useRef(onSelectFrame)
  const framesRef = useRef(frames)
  const fittedRouteRef = useRef('')
  const selectedFrameRef = useRef<string | null>(null)
  const initialCameraRef = useRef(
    cameraTargetForCoordinates(route.map((point) => [point.lon, point.lat] as const)),
  )
  const [ready, setReady] = useState(false)
  const [highContrast, setHighContrast] = useState(false)
  const [mapError, setMapError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const overlay = useOptionalOverlayWorkspace()
  const overlayRef = useRef(overlay)

  onSelectRef.current = onSelectFrame
  framesRef.current = frames
  overlayRef.current = overlay

  const overlayGeoJson = useMemo<FeatureCollection>(
    () => ({
      type: 'FeatureCollection',
      features: (overlay?.mapFeatures ?? []) as FeatureCollection['features'],
    }),
    [overlay?.mapFeatures],
  )
  const overlayMapTotal = (overlay?.layers ?? []).reduce(
    (sum, layer) =>
      overlay?.visibleLayerIds.has(layer.id)
        ? sum + (overlay.features[layer.id]?.wgs84?.total ?? layer.feature_count)
        : sum,
    0,
  )
  const overlayErrorCount = (overlay?.layers ?? []).filter(
    (layer) => overlay?.visibleLayerIds.has(layer.id) && overlay.features[layer.id]?.errorWgs84,
  ).length

  const trackColors = useMemo(() => buildTrackColorMap(route), [route])
  const { effectiveTrackId, showAllTracks: displayAllTracks } = resolveMapTrackScope(
    activeTrackId,
    selectedFrame?.track_id,
    route.find((point) => point.track_id)?.track_id,
    showAllTracks,
  )
  const visibleRoute = useMemo(
    () =>
      displayAllTracks || !effectiveTrackId
        ? route
        : route.filter((point) => point.track_id === effectiveTrackId),
    [displayAllTracks, effectiveTrackId, route],
  )
  const visibleFrames = useMemo(
    () =>
      displayAllTracks || !effectiveTrackId
        ? frames
        : frames.filter((frame) => frame.track_id === effectiveTrackId),
    [displayAllTracks, effectiveTrackId, frames],
  )
  const frameIndexes = useMemo(
    () => new Map(frames.map((frame) => [frame.id, frame.index])),
    [frames],
  )
  const frameGeoJson = useMemo<FeatureCollection>(
    () => ({
      type: 'FeatureCollection',
      features: visibleFrames
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
    [frameRange, selectedFrame, trackColors, visibleFrames],
  )

  const routeGeoJson = useMemo(() => {
    const collection = buildRouteFeatureCollection(route)
    if (displayAllTracks || !effectiveTrackId) return collection
    return {
      ...collection,
      features: collection.features.filter(
        (feature) => feature.properties.track_id === effectiveTrackId,
      ),
    }
  }, [displayAllTracks, effectiveTrackId, route])
  const routeRangeGeoJson = useMemo(() => {
    const collection = buildRouteRangeFeatureCollection(route, frameIndexes, frameRange)
    if (displayAllTracks || !effectiveTrackId) return collection
    return {
      ...collection,
      features: collection.features.filter(
        (feature) => feature.properties.track_id === effectiveTrackId,
      ),
    }
  }, [displayAllTracks, effectiveTrackId, frameIndexes, frameRange, route])

  useEffect(() => {
    const iframe = iframeRef.current
    if (!iframe) return
    let cancelled = false
    let installing = false
    let runtime: VWorldRuntime | null = null
    let dataSources: VWorldSources | null = null
    let resizeObserver: ResizeObserver | null = null
    let mapClickHandler:
      | ((
          windowPosition: unknown,
          ecefPosition: unknown,
          cartographic: { longitudeDD?: number; latitudeDD?: number; height?: number } | null,
        ) => void)
      | null = null

    const install = async () => {
      if (installing || cancelled) return
      const frameWindow = iframe.contentWindow
      if (!frameWindow || !iframe.contentDocument?.getElementById(VWORLD_CONTAINER_ID)) return
      installing = true
      try {
        if (!bootRef.current || bootRef.current.frameWindow !== frameWindow) {
          bootRef.current = {
            frameWindow,
            promise: startVWorldMap(
              frameWindow,
              VWORLD_CONTAINER_ID,
              initialCameraRef.current,
            ),
          }
        }
        runtime = await bootRef.current.promise
        if (cancelled) return
        dataSources = await createVWorldSources(runtime)
        if (cancelled) {
          Object.values(dataSources).forEach((source) =>
            removeVWorldDataSource(runtime!, source),
          )
          return
        }

        runtimeRef.current = runtime
        dataSourcesRef.current = dataSources
        mapClickHandler = (windowPosition, _ecefPosition, cartographic) => {
          if (!runtime) return
          try {
            const entityId = pickedEntityId(runtime.viewer.scene.pick(windowPosition))
            const target = entityId
              ? overlayClickTargetsRef.current.get(entityId) ??
                frameClickTargetsRef.current.get(entityId)
              : undefined
            if (target) {
              target()
              return
            }
          } catch {
            // A terrain click can legitimately have no pickable entity.
          }

          const current = overlayRef.current
          if (!current?.pickMode || !cartographic) return
          const lon = Number(cartographic.longitudeDD)
          const lat = Number(cartographic.latitudeDD)
          const height = Number(cartographic.height ?? 0)
          if (!Number.isFinite(lon) || !Number.isFinite(lat)) return
          void current.applyPickedCoordinate(
            [lon, lat, Number.isFinite(height) ? height : 0],
            'wgs84',
          )
        }
        runtime.map.onClick.addEventListener(mapClickHandler)

        const resize = () => {
          if (!runtime) return
          resizeVWorldMap(runtime, iframe.clientWidth, iframe.clientHeight)
        }
        resizeObserver = new ResizeObserver(resize)
        resizeObserver.observe(iframe)
        frameWindow.requestAnimationFrame(resize)
        setMapError(null)
        setReady(true)
      } catch (reason) {
        if (cancelled) return
        installing = false
        const message = reason instanceof Error ? reason.message : 'VWorld 지도를 초기화하지 못했습니다.'
        setReady(false)
        setMapError(message)
      }
    }

    const onLoad = () => void install()
    iframe.addEventListener('load', onLoad)
    if (iframe.contentDocument?.readyState === 'complete') void install()

    return () => {
      cancelled = true
      iframe.removeEventListener('load', onLoad)
      resizeObserver?.disconnect()
      if (runtime && mapClickHandler) {
        try {
          runtime.map.onClick.removeEventListener(mapClickHandler)
        } catch {
          // The isolated iframe may already be leaving the document.
        }
      }
      if (runtime && dataSources) {
        Object.values(dataSources).forEach((source) => removeVWorldDataSource(runtime!, source))
      }
      try {
        runtime?.map.clear()
      } catch {
        // Removing the iframe releases the VWorld singleton and WebGL context.
      }
      if (runtimeRef.current === runtime) runtimeRef.current = null
      if (dataSourcesRef.current === dataSources) dataSourcesRef.current = null
      frameClickTargetsRef.current = new Map()
      overlayClickTargetsRef.current = new Map()
    }
  }, [reloadToken])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = dataSourcesRef.current?.route
    if (!runtime || !source || !ready) return
    try {
      renderVWorldRoute(runtime, source, routeGeoJson)
    } catch (reason) {
      setMapError(
        reason instanceof Error ? reason.message : 'VWorld 지도 도형을 표시하지 못했습니다.',
      )
    }
  }, [ready, routeGeoJson])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = dataSourcesRef.current?.routeRange
    if (!runtime || !source || !ready) return
    try {
      renderVWorldRouteRange(runtime, source, routeRangeGeoJson)
    } catch (reason) {
      setMapError(
        reason instanceof Error ? reason.message : 'VWorld 선택 구간을 표시하지 못했습니다.',
      )
    }
  }, [ready, routeRangeGeoJson])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = dataSourcesRef.current?.frames
    if (!runtime || !source || !ready) return
    try {
      frameClickTargetsRef.current = renderVWorldFrames(
        runtime,
        source,
        frameGeoJson,
        (frameId) => {
          const frame = framesRef.current.find((candidate) => candidate.id === frameId)
          if (frame) onSelectRef.current(frame)
        },
      )
    } catch (reason) {
      setMapError(
        reason instanceof Error ? reason.message : 'VWorld 프레임을 표시하지 못했습니다.',
      )
    }
  }, [frameGeoJson, ready])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = dataSourcesRef.current?.overlay
    if (!runtime || !source || !ready) return
    try {
      overlayClickTargetsRef.current = renderVWorldOverlay(
        runtime,
        source,
        overlayGeoJson,
        (layerId, featureId) => {
          overlayRef.current?.selectFeature({ layerId, featureId })
        },
      )
    } catch (reason) {
      setMapError(
        reason instanceof Error ? reason.message : 'VWorld SHP 피처를 표시하지 못했습니다.',
      )
    }
  }, [overlayGeoJson, ready])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime || !ready || visibleRoute.length === 0) return
    const routeKey = `${effectiveTrackId ?? 'all'}:${displayAllTracks}:${visibleRoute[0]?.frame_id ?? ''}:${visibleRoute.at(-1)?.frame_id ?? ''}:${visibleRoute.length}`
    if (fittedRouteRef.current === routeKey) return
    fittedRouteRef.current = routeKey
    moveVWorldMap(
      runtime,
      cameraTargetForCoordinates(
        visibleRoute.map((point) => [point.lon, point.lat] as const),
      ),
    )
  }, [displayAllTracks, effectiveTrackId, ready, visibleRoute])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!selectedFrame?.coordinate) {
      selectedFrameRef.current = null
      return
    }
    if (selectedFrameRef.current === null) {
      selectedFrameRef.current = selectedFrame.id
      return
    }
    if (!runtime || !ready || selectedFrameRef.current === selectedFrame.id) return
    selectedFrameRef.current = selectedFrame.id
    moveVWorldMap(runtime, {
      lon: selectedFrame.coordinate.lon,
      lat: selectedFrame.coordinate.lat,
      height: 420,
      heading: -(selectedFrame.heading ?? 0),
      tilt: -65,
    })
  }, [ready, selectedFrame])

  const recenter = () => {
    const runtime = runtimeRef.current
    if (!selectedFrame?.coordinate || !runtime) return
    moveVWorldMap(runtime, {
      lon: selectedFrame.coordinate.lon,
      lat: selectedFrame.coordinate.lat,
      height: 280,
      heading: -(selectedFrame.heading ?? 0),
      tilt: -62,
    })
  }

  const retry = () => {
    bootRef.current = null
    fittedRouteRef.current = ''
    setMapError(null)
    setReady(false)
    setReloadToken((value) => value + 1)
  }

  return (
    <div
      className={`map-view ${highContrast ? 'high-contrast-mode' : ''}`}
      data-map-provider="vworld-webgl-3.0"
      data-track-scope={displayAllTracks ? 'all' : effectiveTrackId ?? 'none'}
      data-route-feature-count={routeGeoJson.features.length}
      data-overlay-feature-count={overlayGeoJson.features.length}
    >
      <iframe
        key={reloadToken}
        ref={iframeRef}
        className="map-container vworld-map-frame"
        src={`${VWORLD_IFRAME_URL}?reload=${reloadToken}`}
        title="VWorld WebGL 3D 지도"
      />
      {(!ready || loading) && !mapError && (
        <div className="map-loading" role="status">
          <LoaderCircle size={15} className="spin" />
          {!ready ? 'VWorld 지도 엔진 준비 중' : '이동 경로 불러오는 중'}
        </div>
      )}
      {mapError && (
        <div className="map-provider-error" role="alert">
          <AlertTriangle size={18} />
          <span>
            <strong>VWorld 지도를 열 수 없습니다</strong>
            <small>{mapError}</small>
          </span>
          <button type="button" onClick={retry}>
            <RotateCcw size={14} /> 다시 시도
          </button>
        </div>
      )}
      <div className="map-tools">
        <button type="button" onClick={recenter} disabled={!ready || !selectedFrame?.coordinate}>
          <Crosshair size={16} />
          현재 프레임
        </button>
        <button type="button" onClick={() => setHighContrast((value) => !value)} disabled={!ready}>
          <Layers size={16} />
          {highContrast ? '기본 톤' : '고대비'}
        </button>
      </div>
      <div className="map-legend">
        <span className="map-provider-badge">VWorld 3D</span>
        <span>
          <i
            className="legend-route"
            style={
              !displayAllTracks && effectiveTrackId
                ? { background: trackColors.get(effectiveTrackId) ?? TRACK_COLORS[0] }
                : undefined
            }
          />
          {displayAllTracks ? '전체 트랙' : '활성 트랙'}
        </span>
        <span>
          <i className="legend-frame" />
          MMS 프레임
        </span>
        {overlayGeoJson.features.length > 0 && (
          <span
            title={
              overlayMapTotal > overlayGeoJson.features.length
                ? '속성표에서 다음 피처를 불러오면 지도 표시도 확장됩니다.'
                : '표시 중인 SHP 피처'
            }
          >
            SHP {overlayGeoJson.features.length.toLocaleString('ko-KR')}
            {overlayMapTotal > overlayGeoJson.features.length
              ? ` / ${overlayMapTotal.toLocaleString('ko-KR')} 미리보기`
              : ''}
          </span>
        )}
        {overlayErrorCount > 0 && (
          <span className="map-overlay-error" title="SHP 검수 패널에서 다시 불러올 수 있습니다.">
            SHP 로드 오류 {overlayErrorCount}개
          </span>
        )}
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
