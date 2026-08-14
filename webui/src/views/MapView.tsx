import { AlertTriangle, Box, Crosshair, Image as ImageIcon, Layers, LoaderCircle, Map as MapIcon, Navigation2, RotateCcw } from 'lucide-react'
import type { FeatureCollection } from 'geojson'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  openOverlayFeatureDetails,
  OverlayHoverTooltip,
  type OverlayHoverState,
} from '../components/OverlayHoverTooltip'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import { frameNavigationDirection, isTextEntryTarget } from '../lib/frameNavigation'
import {
  MAP_SELECTED_FEATURE_COLOR,
  MAP_SELECTED_FRAME_COLOR,
} from '../lib/mapSelectionColors'
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
  pickedEntityIdsAtPosition,
  removeVWorldDataSource,
  renderVWorldFrames,
  renderVWorldOverlay,
  renderVWorldRoute,
  renderVWorldRouteRange,
  resizeVWorldMap,
  setVWorldSceneMode,
  startVWorldMap,
  VWORLD_CONTAINER_ID,
  VWORLD_IFRAME_URL,
  type VWorldCustomDataSource,
  type VWorldOverlayHoverTarget,
  type VWorldRuntime,
} from '../lib/vworld'

export function relayMapOverlayShortcut(event: KeyboardEvent, ownerWindow: Window): boolean {
  const globalShortcut =
    frameNavigationDirection(event) !== null ||
    event.code === 'KeyN' ||
    event.code === 'KeyP' ||
    event.key === 'Escape'
  if (
    event.defaultPrevented ||
    !globalShortcut ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    isTextEntryTarget(event.target)
  ) {
    return false
  }
  const KeyboardEventConstructor = ownerWindow.document.defaultView?.KeyboardEvent ?? KeyboardEvent
  const relayedEvent = new KeyboardEventConstructor('keydown', {
    key: event.key,
    code: event.code,
    repeat: event.repeat,
    bubbles: true,
    cancelable: true,
  })
  const handled = !ownerWindow.dispatchEvent(relayedEvent)
  if (handled) event.preventDefault()
  return handled
}
import {
  createVWorld2DDataSource,
  destroyVWorld2DMap,
  fitVWorld2DMap,
  handleVWorld2DClick,
  moveVWorld2DMap,
  removeVWorld2DDataSource,
  renderVWorld2DCollection,
  setVWorld2DBaseMap,
  startVWorld2DMap,
  vworld2DOverlayHoverTarget,
  VWORLD_2D_CONTAINER_ID,
  VWORLD_2D_IFRAME_URL,
  type VWorld2DBaseMap,
  type VWorld2DDataSource,
  type VWorld2DRuntime,
} from '../lib/vworld2d'
import type { Frame, FrameRange, RoutePoint, SurveySegment } from '../types'

const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: 'FeatureCollection',
  features: [],
}

export function collectionForMapLayer(
  collection: FeatureCollection,
  visible: boolean,
): FeatureCollection {
  return visible ? collection : EMPTY_FEATURE_COLLECTION
}

export type MapMode = '2d' | 'satellite' | '3d'

export function isVWorld2DMapMode(mode: MapMode): mode is Exclude<MapMode, '3d'> {
  return mode !== '3d'
}

export function vworld2DBaseMapForMode(mode: MapMode): VWorld2DBaseMap {
  return mode === 'satellite' ? 'satellite' : 'base'
}

export function mapProviderForMode(mode: MapMode): string {
  if (mode === '3d') return 'vworld-webgl-3.0'
  return mode === 'satellite'
    ? 'vworld-wmts-satellite-1.0.0'
    : 'vworld-wmts-base-1.0.0'
}

export function filterMapTracks<T extends { track_id?: string }>(
  items: readonly T[],
  visibleTrackIds: ReadonlySet<string>,
): T[] {
  return items.filter((item) => Boolean(item.track_id && visibleTrackIds.has(item.track_id)))
}

export function buildSurveyFeatureCollection(
  segments: readonly SurveySegment[],
  draft: readonly [number, number][],
  draftColor: string,
): FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: [
      ...segments.map((segment) => ({
        type: 'Feature' as const,
        id: segment.id,
        properties: {
          track_color: segment.color,
          survey_segment_id: segment.id,
          survey_name: segment.name,
        },
        geometry: segment.geometry,
      })),
      ...(draft.length >= 2
        ? [{
            type: 'Feature' as const,
            id: 'survey-draft',
            properties: { track_color: draftColor, survey_draft: 1 },
            geometry: { type: 'LineString' as const, coordinates: [...draft] },
          }]
        : []),
    ],
  }
}

interface EntityTargetCollection {
  has(entityId: string): boolean
}

export function firstTargetEntityId(
  entityIds: readonly string[],
  targetCollections: readonly EntityTargetCollection[],
): string | null {
  return entityIds.find((entityId) =>
    targetCollections.some((targets) => targets.has(entityId))) ?? null
}

interface MapViewProps {
  route: RoutePoint[]
  frames: Frame[]
  selectedFrame: Frame | null
  activeTrackId?: string
  showAllTracks?: boolean
  trackLayerVisible?: boolean
  visibleTrackIds?: ReadonlySet<string>
  trackOrder?: readonly string[]
  frameRange?: FrameRange | null
  loading: boolean
  mapMode: MapMode
  onMapModeChange: (mode: MapMode) => void
  onSelectFrame: (frame: Frame) => void
  surveySegments?: SurveySegment[]
  surveyDraft?: [number, number][]
  surveyDraftColor?: string
  surveyDrawing?: boolean
  onAddSurveyPoint?: (coordinate: [number, number]) => void
}

interface VWorldBoot {
  frameWindow: Window
  promise: Promise<VWorldRuntime>
}

interface VWorld2DBoot {
  frameWindow: Window
  promise: Promise<VWorld2DRuntime>
}

interface VWorldSources {
  route: VWorldCustomDataSource
  routeRange: VWorldCustomDataSource
  frames: VWorldCustomDataSource
  overlay: VWorldCustomDataSource
  survey: VWorldCustomDataSource
}

interface VWorld2DSources {
  route: VWorld2DDataSource
  routeRange: VWorld2DDataSource
  frames: VWorld2DDataSource
  overlay: VWorld2DDataSource
  survey: VWorld2DDataSource
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
      survey: await create('mms-field-survey'),
    }
  } catch (reason) {
    created.forEach((source) => removeVWorldDataSource(runtime, source))
    throw reason
  }
}

function createVWorld2DSources(runtime: VWorld2DRuntime): VWorld2DSources {
  return {
    route: createVWorld2DDataSource(runtime),
    routeRange: createVWorld2DDataSource(runtime),
    frames: createVWorld2DDataSource(runtime),
    overlay: createVWorld2DDataSource(runtime),
    survey: createVWorld2DDataSource(runtime),
  }
}

export type FrameNavigationIntent = 'selection-change' | 'current-frame-button'

export function frameNavigationTarget(
  frame: Frame | null,
  intent: FrameNavigationIntent,
) {
  if (intent !== 'current-frame-button' || !frame?.coordinate) return null
  return {
    lon: frame.coordinate.lon,
    lat: frame.coordinate.lat,
    height: 280,
    heading: -(frame.heading ?? 0),
    tilt: -62,
  }
}

export function MapView({
  route,
  frames,
  selectedFrame,
  activeTrackId,
  showAllTracks = false,
  trackLayerVisible = true,
  visibleTrackIds,
  trackOrder = [],
  frameRange,
  loading,
  mapMode,
  onMapModeChange,
  onSelectFrame,
  surveySegments = [],
  surveyDraft = [],
  surveyDraftColor = '#f59e0b',
  surveyDrawing = false,
  onAddSurveyPoint,
}: MapViewProps) {
  const mapRootRef = useRef<HTMLDivElement>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const iframe2DRef = useRef<HTMLIFrameElement>(null)
  const bootRef = useRef<VWorldBoot | null>(null)
  const boot2DRef = useRef<VWorld2DBoot | null>(null)
  const runtimeRef = useRef<VWorldRuntime | null>(null)
  const runtime2DRef = useRef<VWorld2DRuntime | null>(null)
  const sourcesRef = useRef<VWorldSources | null>(null)
  const sources2DRef = useRef<VWorld2DSources | null>(null)
  const frameClickTargetsRef = useRef<ReadonlyMap<string, () => void>>(new Map())
  const overlayClickTargetsRef = useRef<ReadonlyMap<string, () => void>>(new Map())
  const overlayHoverTargetsRef = useRef<ReadonlyMap<string, VWorldOverlayHoverTarget>>(new Map())
  const onSelectRef = useRef(onSelectFrame)
  const framesRef = useRef(frames)
  const fittedRouteRef = useRef('')
  const initialCameraRef = useRef(
    cameraTargetForCoordinates(route.map((point) => [point.lon, point.lat] as const)),
  )
  const [ready3D, setReady3D] = useState(false)
  const [ready2D, setReady2D] = useState(false)
  const [highContrast, setHighContrast] = useState(false)
  const [mapError, setMapError] = useState<string | null>(null)
  const [mapHover, setMapHover] = useState<OverlayHoverState | null>(null)
  const [pinnedMapHover, setPinnedMapHover] = useState<OverlayHoverState | null>(null)
  const mapHoverRef = useRef<OverlayHoverState | null>(null)
  const pinnedMapHoverRef = useRef<OverlayHoverState | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const overlay = useOptionalOverlayWorkspace()
  const overlayRef = useRef(overlay)
  const mapModeRef = useRef(mapMode)
  const surveyDrawingRef = useRef(surveyDrawing)
  const onAddSurveyPointRef = useRef(onAddSurveyPoint)

  onSelectRef.current = onSelectFrame
  framesRef.current = frames
  overlayRef.current = overlay
  mapModeRef.current = mapMode
  surveyDrawingRef.current = surveyDrawing
  onAddSurveyPointRef.current = onAddSurveyPoint
  mapHoverRef.current = mapHover
  pinnedMapHoverRef.current = pinnedMapHover
  const vworld2DActive = isVWorld2DMapMode(mapMode)
  const ready = vworld2DActive ? ready2D : ready3D

  const overlayGeoJson = useMemo<FeatureCollection>(
    () => ({
      type: 'FeatureCollection',
      features: (overlay?.mapFeatures ?? []) as FeatureCollection['features'],
    }),
    [overlay?.mapFeatures],
  )
  const surveyGeoJson = useMemo(
    () => buildSurveyFeatureCollection(surveySegments, surveyDraft, surveyDraftColor),
    [surveyDraft, surveyDraftColor, surveySegments],
  )
  const selectedOverlayCoordinate = useMemo(() => {
    const geometry = overlay?.selectedFeature?.geometry
    if (geometry?.type !== 'Point' || !Array.isArray(geometry.coordinates)) return null
    const lon = Number(geometry.coordinates[0])
    const lat = Number(geometry.coordinates[1])
    return Number.isFinite(lon) && Number.isFinite(lat) ? { lon, lat } : null
  }, [overlay?.selectedFeature])
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

  const trackOrderKey = trackOrder.join('\u0000')
  const trackColors = useMemo(
    () => buildTrackColorMap(route, trackOrder),
    // The ordered ids are the semantic dependency; callers need not memoize the array.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [route, trackOrderKey],
  )
  const { effectiveTrackId, showAllTracks: displayAllTracks } = resolveMapTrackScope(
    activeTrackId,
    selectedFrame?.track_id,
    route.find((point) => point.track_id)?.track_id,
    showAllTracks,
  )
  const visibleRoute = useMemo(
    () =>
      visibleTrackIds
        ? filterMapTracks(route, visibleTrackIds)
        : displayAllTracks || !effectiveTrackId
          ? route
          : route.filter((point) => point.track_id === effectiveTrackId),
    [displayAllTracks, effectiveTrackId, route, visibleTrackIds],
  )
  const visibleFrames = useMemo(
    () =>
      visibleTrackIds
        ? filterMapTracks(frames, visibleTrackIds)
        : displayAllTracks || !effectiveTrackId
          ? frames
          : frames.filter((frame) => frame.track_id === effectiveTrackId),
    [displayAllTracks, effectiveTrackId, frames, visibleTrackIds],
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

  const hasVisibleTracks = visibleTrackIds ? visibleTrackIds.size > 0 : trackLayerVisible
  const renderedFrameGeoJson = collectionForMapLayer(frameGeoJson, hasVisibleTracks)

  const routeGeoJson = useMemo(() => {
    return buildRouteFeatureCollection(visibleRoute, trackColors)
  }, [trackColors, visibleRoute])
  const routeRangeGeoJson = useMemo(
    () => buildRouteRangeFeatureCollection(visibleRoute, frameIndexes, frameRange, trackColors),
    [frameIndexes, frameRange, trackColors, visibleRoute],
  )
  const renderedRouteGeoJson = collectionForMapLayer(routeGeoJson, hasVisibleTracks)
  const renderedRouteRangeGeoJson = collectionForMapLayer(routeRangeGeoJson, hasVisibleTracks)

  useEffect(() => {
    if (mapMode !== '3d') return
    const iframe = iframeRef.current
    if (!iframe) return
    let cancelled = false
    let installing = false
    let runtime: VWorldRuntime | null = null
    let sources: VWorldSources | null = null
    let resizeObserver: ResizeObserver | null = null
    let hoverCanvas: HTMLCanvasElement | null = null
    let hoverAnimationFrame = 0
    let pendingHover: { x: number; y: number } | null = null
    let mapPointerMoveHandler: ((event: PointerEvent) => void) | null = null
    let mapPointerLeaveHandler: (() => void) | null = null
    let mapKeyDownDocument: Document | null = null
    let mapKeyDownHandler: ((event: KeyboardEvent) => void) | null = null
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
        sources = await createVWorldSources(runtime)
        if (cancelled) {
          Object.values(sources).forEach((source) => removeVWorldDataSource(runtime!, source))
          return
        }
        runtimeRef.current = runtime
        sourcesRef.current = sources
        mapClickHandler = (windowPosition, _ecefPosition, cartographic) => {
          if (!runtime) return
          if (surveyDrawingRef.current && !overlayRef.current?.pickMode && cartographic) {
            const longitude = Number(cartographic.longitudeDD)
            const latitude = Number(cartographic.latitudeDD)
            if (Number.isFinite(longitude) && Number.isFinite(latitude)) {
              onAddSurveyPointRef.current?.([longitude, latitude])
              return
            }
          }
          try {
            const entityId = firstTargetEntityId(
              pickedEntityIdsAtPosition(runtime.viewer.scene, windowPosition),
              [
                overlayHoverTargetsRef.current,
                frameClickTargetsRef.current,
                overlayClickTargetsRef.current,
              ],
            )
            const hoverTarget = entityId
              ? overlayHoverTargetsRef.current.get(entityId)
              : undefined
            if (hoverTarget) {
              const transient = mapHoverRef.current
              const x = Number((windowPosition as { x?: unknown })?.x ?? 0)
              const y = Number((windowPosition as { y?: unknown })?.y ?? 0)
              setPinnedMapHover(
                transient?.layerId === hoverTarget.layerId &&
                  String(transient.featureId) === String(hoverTarget.featureId)
                  ? transient
                  : {
                      layerId: hoverTarget.layerId,
                      layerName: overlayRef.current?.layers.find(
                        (layer) => layer.id === hoverTarget.layerId,
                      )?.name ?? hoverTarget.layerId,
                      featureId: hoverTarget.featureId,
                      properties: hoverTarget.properties,
                      x: Number.isFinite(x) ? x : 0,
                      y: Number.isFinite(y) ? y : 0,
                      viewportWidth: iframe.clientWidth,
                      viewportHeight: iframe.clientHeight,
                    },
              )
              return
            }
            setPinnedMapHover(null)
            setMapHover(null)
            const target = entityId
              ? frameClickTargetsRef.current.get(entityId)
              : undefined
            if (target) {
              target()
              return
            }
            const overlayTarget = entityId
              ? overlayClickTargetsRef.current.get(entityId)
              : undefined
            if (overlayTarget) {
              overlayTarget()
              return
            }
          } catch {
            // A terrain click can legitimately have no pickable entity.
          }

          if (!cartographic) return
          const lon = Number(cartographic.longitudeDD)
          const lat = Number(cartographic.latitudeDD)
          const height = Number(cartographic.height ?? 0)
          if (!Number.isFinite(lon) || !Number.isFinite(lat)) return
          const current = overlayRef.current
          if (current?.pickMode) {
            void current.applyPickedCoordinate(
              [lon, lat, Number.isFinite(height) ? height : 0],
              'wgs84',
            )
          } else if (surveyDrawingRef.current) {
            onAddSurveyPointRef.current?.([lon, lat])
          }
        }
        runtime.map.onClick.addEventListener(mapClickHandler)

        hoverCanvas = runtime.viewer.scene.canvas
        const updateHover = () => {
          hoverAnimationFrame = 0
          const pending = pendingHover
          pendingHover = null
          if (!runtime || !pending) return
          let target: VWorldOverlayHoverTarget | undefined
          try {
            const entityId = firstTargetEntityId(
              pickedEntityIdsAtPosition(runtime.viewer.scene, { x: pending.x, y: pending.y }),
              [overlayHoverTargetsRef.current],
            )
            target = entityId ? overlayHoverTargetsRef.current.get(entityId) : undefined
          } catch {
            target = undefined
          }
          if (!target) {
            setMapHover(null)
            return
          }
          const layerName = overlayRef.current?.layers.find(
            (layer) => layer.id === target?.layerId,
          )?.name ?? target.layerId
          setMapHover({
            layerId: target.layerId,
            layerName,
            featureId: target.featureId,
            properties: target.properties,
            x: pending.x,
            y: pending.y,
            viewportWidth: iframe.clientWidth,
            viewportHeight: iframe.clientHeight,
          })
        }
        mapPointerMoveHandler = (event) => {
          if (pinnedMapHoverRef.current) return
          pendingHover = { x: event.offsetX, y: event.offsetY }
          if (!hoverAnimationFrame) {
            hoverAnimationFrame = frameWindow.requestAnimationFrame(updateHover)
          }
        }
        mapPointerLeaveHandler = () => {
          pendingHover = null
          if (!pinnedMapHoverRef.current) setMapHover(null)
        }
        hoverCanvas.addEventListener('pointermove', mapPointerMoveHandler)
        hoverCanvas.addEventListener('pointerleave', mapPointerLeaveHandler)
        mapKeyDownDocument = frameWindow.document
        mapKeyDownHandler = (event) => {
          const relayed = relayMapOverlayShortcut(event, iframe.ownerDocument.defaultView ?? window)
          if (event.key === 'Escape' && pinnedMapHoverRef.current) {
            event.preventDefault()
            setPinnedMapHover(null)
            setMapHover(null)
          }
          if (relayed) return
        }
        // VWorld/OpenLayers may consume keyboard input on the focused canvas.
        // Capture first so shortcuts still reach the application after a SHP
        // point-pick leaves focus inside this isolated iframe document.
        mapKeyDownDocument.addEventListener('keydown', mapKeyDownHandler, true)

        const resize = () => {
          if (!runtime) return
          resizeVWorldMap(runtime, iframe.clientWidth, iframe.clientHeight)
        }
        resizeObserver = new ResizeObserver(resize)
        resizeObserver.observe(iframe)
        frameWindow.requestAnimationFrame(resize)
        setMapError(null)
        setReady3D(true)
      } catch (reason) {
        if (cancelled) return
        installing = false
        const message = reason instanceof Error ? reason.message : 'VWorld 지도를 초기화하지 못했습니다.'
        setReady3D(false)
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
      if (hoverCanvas && mapPointerMoveHandler) {
        hoverCanvas.removeEventListener('pointermove', mapPointerMoveHandler)
      }
      if (hoverCanvas && mapPointerLeaveHandler) {
        hoverCanvas.removeEventListener('pointerleave', mapPointerLeaveHandler)
      }
      if (hoverAnimationFrame) iframe.contentWindow?.cancelAnimationFrame(hoverAnimationFrame)
      if (mapKeyDownDocument && mapKeyDownHandler) {
        mapKeyDownDocument.removeEventListener('keydown', mapKeyDownHandler, true)
      }
      if (runtime && mapClickHandler) {
        try {
          runtime.map.onClick.removeEventListener(mapClickHandler)
        } catch {
          // The isolated iframe may already be leaving the document.
        }
      }
      if (runtime && sources) {
        Object.values(sources).forEach((source) => removeVWorldDataSource(runtime!, source))
      }
      try {
        runtime?.map.clear()
      } catch {
        // Removing the iframe releases the VWorld singleton and WebGL context.
      }
      if (runtimeRef.current === runtime) runtimeRef.current = null
      if (sourcesRef.current === sources) sourcesRef.current = null
      frameClickTargetsRef.current = new Map()
      overlayClickTargetsRef.current = new Map()
      overlayHoverTargetsRef.current = new Map()
      setMapHover(null)
      setReady3D(false)
    }
  }, [mapMode, reloadToken])

  useEffect(() => {
    if (!vworld2DActive) return
    const iframe = iframe2DRef.current
    if (!iframe) return
    let cancelled = false
    let installing = false
    let runtime: VWorld2DRuntime | null = null
    let sources: VWorld2DSources | null = null
    let resizeObserver: ResizeObserver | null = null
    let mapClickHandler: ((event: { pixel?: unknown; coordinate?: unknown }) => void) | null = null
    let mapPointerMoveHandler: ((event: { pixel?: unknown; coordinate?: unknown }) => void) | null = null
    let mapKeyDownDocument: Document | null = null
    let mapKeyDownHandler: ((event: KeyboardEvent) => void) | null = null

    const install = async () => {
      if (installing || cancelled) return
      const frameWindow = iframe.contentWindow
      if (!frameWindow || !iframe.contentDocument?.getElementById(VWORLD_2D_CONTAINER_ID)) return
      installing = true
      try {
        if (!boot2DRef.current || boot2DRef.current.frameWindow !== frameWindow) {
          boot2DRef.current = {
            frameWindow,
            promise: startVWorld2DMap(
              frameWindow,
              VWORLD_2D_CONTAINER_ID,
              initialCameraRef.current,
              15_000,
              vworld2DBaseMapForMode(mapModeRef.current),
            ),
          }
        }
        runtime = await boot2DRef.current.promise
        // React Strict Mode can immediately clean up the first effect and let
        // the second effect reuse this same boot promise. Do not destroy the
        // shared runtime from the cancelled continuation; the active effect or
        // iframe removal owns its lifecycle.
        if (cancelled) return
        sources = createVWorld2DSources(runtime)
        if (cancelled) {
          Object.values(sources).forEach((source) => removeVWorld2DDataSource(runtime!, source))
          return
        }
        runtime2DRef.current = runtime
        sources2DRef.current = sources
        mapClickHandler = (event) => {
          if (!runtime) return
          if (
            surveyDrawingRef.current &&
            !overlayRef.current?.pickMode &&
            event.coordinate !== undefined
          ) {
            const [longitude, latitude] = runtime.ol.proj.toLonLat(event.coordinate)
            if (Number.isFinite(longitude) && Number.isFinite(latitude)) {
              onAddSurveyPointRef.current?.([longitude, latitude])
              return
            }
          }
          const hoverTarget = vworld2DOverlayHoverTarget(runtime, event)
          if (hoverTarget) {
            const layerName = overlayRef.current?.layers.find(
              (layer) => layer.id === hoverTarget.layerId,
            )?.name ?? hoverTarget.layerId
            const transient = mapHoverRef.current
            setPinnedMapHover(
              transient?.layerId === hoverTarget.layerId &&
                String(transient.featureId) === String(hoverTarget.featureId)
                ? transient
                : {
                    layerId: hoverTarget.layerId,
                    layerName,
                    featureId: hoverTarget.featureId,
                    properties: hoverTarget.properties,
                    x: hoverTarget.pixel[0],
                    y: hoverTarget.pixel[1],
                    viewportWidth: iframe.clientWidth,
                    viewportHeight: iframe.clientHeight,
                  },
            )
            return
          }
          setPinnedMapHover(null)
          setMapHover(null)
          handleVWorld2DClick(
            runtime,
            event,
            {
              onFrame: (frameId) => {
                const frame = framesRef.current.find((candidate) => candidate.id === frameId)
                if (frame) onSelectRef.current(frame)
              },
              onOverlay: (layerId, featureId) => {
                overlayRef.current?.selectFeature({ layerId, featureId })
              },
            },
            (coordinate) => {
              const current = overlayRef.current
              if (current?.pickMode) void current.applyPickedCoordinate(coordinate, 'wgs84')
            },
          )
        }
        runtime.map.on('singleclick', mapClickHandler)
        mapPointerMoveHandler = (event) => {
          if (!runtime) return
          if (pinnedMapHoverRef.current) return
          const target = vworld2DOverlayHoverTarget(runtime, event)
          if (!target) {
            setMapHover(null)
            return
          }
          const layerName = overlayRef.current?.layers.find(
            (layer) => layer.id === target.layerId,
          )?.name ?? target.layerId
          setMapHover({
            layerId: target.layerId,
            layerName,
            featureId: target.featureId,
            properties: target.properties,
            x: target.pixel[0],
            y: target.pixel[1],
            viewportWidth: iframe.clientWidth,
            viewportHeight: iframe.clientHeight,
          })
        }
        runtime.map.on('pointermove', mapPointerMoveHandler)
        mapKeyDownDocument = frameWindow.document
        mapKeyDownHandler = (event) => {
          const relayed = relayMapOverlayShortcut(event, iframe.ownerDocument.defaultView ?? window)
          if (event.key === 'Escape' && pinnedMapHoverRef.current) {
            event.preventDefault()
            setPinnedMapHover(null)
            setMapHover(null)
          }
          if (relayed) return
        }
        mapKeyDownDocument.addEventListener('keydown', mapKeyDownHandler, true)

        const resize = () => runtime?.map.updateSize()
        resizeObserver = new ResizeObserver(resize)
        resizeObserver.observe(iframe)
        frameWindow.requestAnimationFrame(resize)
        setMapError(null)
        setReady2D(true)
      } catch (reason) {
        if (cancelled) return
        installing = false
        setReady2D(false)
        setMapError(
          reason instanceof Error ? reason.message : 'VWorld 2D 일반지도를 초기화하지 못했습니다.',
        )
      }
    }

    const onLoad = () => void install()
    iframe.addEventListener('load', onLoad)
    if (iframe.contentDocument?.readyState === 'complete') void install()

    return () => {
      cancelled = true
      iframe.removeEventListener('load', onLoad)
      resizeObserver?.disconnect()
      if (mapKeyDownDocument && mapKeyDownHandler) {
        mapKeyDownDocument.removeEventListener('keydown', mapKeyDownHandler, true)
      }
      if (runtime && mapClickHandler) {
        try {
          runtime.map.un('singleclick', mapClickHandler)
        } catch {
          // The isolated 2D frame may already be leaving the document.
        }
      }
      if (runtime && mapPointerMoveHandler) {
        try {
          runtime.map.un('pointermove', mapPointerMoveHandler)
        } catch {
          // The isolated 2D frame may already be leaving the document.
        }
      }
      if (runtime && sources) {
        Object.values(sources).forEach((source) => removeVWorld2DDataSource(runtime!, source))
      }
      if (runtime) destroyVWorld2DMap(runtime)
      if (runtime2DRef.current === runtime) runtime2DRef.current = null
      if (sources2DRef.current === sources) sources2DRef.current = null
      setMapHover(null)
      setReady2D(false)
    }
  }, [reloadToken, vworld2DActive])

  useEffect(() => {
    const runtime = runtime2DRef.current
    if (!runtime || !ready2D || !vworld2DActive) return
    setVWorld2DBaseMap(runtime, vworld2DBaseMapForMode(mapMode))
  }, [mapMode, ready2D, vworld2DActive])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = sourcesRef.current?.route
    if (!runtime || !source || !ready3D) return
    try {
      renderVWorldRoute(runtime, source, renderedRouteGeoJson)
    } catch (reason) {
      setMapError(reason instanceof Error ? reason.message : 'VWorld 이동 경로를 갱신하지 못했습니다.')
    }
  }, [ready3D, renderedRouteGeoJson])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = sourcesRef.current?.routeRange
    if (!runtime || !source || !ready3D) return
    try {
      renderVWorldRouteRange(runtime, source, renderedRouteRangeGeoJson)
    } catch (reason) {
      setMapError(reason instanceof Error ? reason.message : 'VWorld 선택 구간을 갱신하지 못했습니다.')
    }
  }, [ready3D, renderedRouteRangeGeoJson])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = sourcesRef.current?.frames
    if (!runtime || !source || !ready3D) return
    try {
      frameClickTargetsRef.current = renderVWorldFrames(runtime, source, renderedFrameGeoJson, (frameId) => {
        const frame = framesRef.current.find((candidate) => candidate.id === frameId)
        if (frame) onSelectRef.current(frame)
      })
    } catch (reason) {
      setMapError(reason instanceof Error ? reason.message : 'VWorld 프레임을 갱신하지 못했습니다.')
    }
  }, [ready3D, renderedFrameGeoJson])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = sourcesRef.current?.overlay
    if (!runtime || !source || !ready3D) return
    try {
      const hoverTargets = new Map<string, VWorldOverlayHoverTarget>()
      overlayClickTargetsRef.current = renderVWorldOverlay(runtime, source, overlayGeoJson, (layerId, featureId) => {
        overlayRef.current?.selectFeature({ layerId, featureId })
      }, hoverTargets)
      overlayHoverTargetsRef.current = hoverTargets
    } catch (reason) {
      setMapError(reason instanceof Error ? reason.message : 'VWorld SHP를 갱신하지 못했습니다.')
    }
  }, [overlayGeoJson, ready3D])

  useEffect(() => {
    const runtime = runtime2DRef.current
    const source = sources2DRef.current?.route
    if (!runtime || !source || !ready2D) return
    renderVWorld2DCollection(runtime, source, renderedRouteGeoJson, 'route')
  }, [ready2D, renderedRouteGeoJson])

  useEffect(() => {
    const runtime = runtime2DRef.current
    const source = sources2DRef.current?.routeRange
    if (!runtime || !source || !ready2D) return
    renderVWorld2DCollection(runtime, source, renderedRouteRangeGeoJson, 'route-range')
  }, [ready2D, renderedRouteRangeGeoJson])

  useEffect(() => {
    const runtime = runtime2DRef.current
    const source = sources2DRef.current?.frames
    if (!runtime || !source || !ready2D) return
    renderVWorld2DCollection(runtime, source, renderedFrameGeoJson, 'frame')
  }, [ready2D, renderedFrameGeoJson])

  useEffect(() => {
    const runtime = runtime2DRef.current
    const source = sources2DRef.current?.overlay
    if (!runtime || !source || !ready2D) return
    renderVWorld2DCollection(runtime, source, overlayGeoJson, 'overlay')
  }, [overlayGeoJson, ready2D])

  useEffect(() => {
    const runtime = runtimeRef.current
    const source = sourcesRef.current?.survey
    if (!runtime || !source || !ready3D) return
    try {
      renderVWorldRoute(runtime, source, surveyGeoJson)
    } catch (reason) {
      setMapError(reason instanceof Error ? reason.message : '현장조사 구간을 갱신하지 못했습니다.')
    }
  }, [ready3D, surveyGeoJson])

  useEffect(() => {
    const runtime = runtime2DRef.current
    const source = sources2DRef.current?.survey
    if (!runtime || !source || !ready2D) return
    renderVWorld2DCollection(runtime, source, surveyGeoJson, 'route')
  }, [ready2D, surveyGeoJson])

  useEffect(() => {
    if (!ready || !hasVisibleTracks || visibleRoute.length === 0) return
    const routeKey = `${visibleTrackIds ? [...visibleTrackIds].sort().join(',') : effectiveTrackId ?? 'all'}:${visibleRoute[0]?.frame_id ?? ''}:${visibleRoute.at(-1)?.frame_id ?? ''}:${visibleRoute.length}`
    if (fittedRouteRef.current === routeKey) return
    fittedRouteRef.current = routeKey
    const coordinates = visibleRoute.map((point) => [point.lon, point.lat] as const)
    if (vworld2DActive) {
      const runtime = runtime2DRef.current
      if (runtime) fitVWorld2DMap(runtime, coordinates)
    } else {
      const runtime = runtimeRef.current
      if (runtime) setVWorldSceneMode(runtime, '3d', cameraTargetForCoordinates(coordinates))
    }
  }, [effectiveTrackId, hasVisibleTracks, ready, visibleRoute, visibleTrackIds, vworld2DActive])

  useEffect(() => {
    if (!ready || !selectedOverlayCoordinate) return
    const target = {
      ...selectedOverlayCoordinate,
      height: vworld2DActive ? 650 : 180,
      heading: 0,
      tilt: vworld2DActive ? -90 : -64,
    }
    if (vworld2DActive) {
      const runtime = runtime2DRef.current
      if (runtime) moveVWorld2DMap(runtime, target)
    } else {
      const runtime = runtimeRef.current
      if (runtime) moveVWorldMap(runtime, target)
    }
  }, [ready, selectedOverlayCoordinate, vworld2DActive])

  const recenter = () => {
    const target = frameNavigationTarget(selectedFrame, 'current-frame-button')
    if (!target) return
    if (vworld2DActive) {
      const runtime = runtime2DRef.current
      if (runtime) moveVWorld2DMap(runtime, target)
    } else {
      const runtime = runtimeRef.current
      if (runtime) setVWorldSceneMode(runtime, '3d', target)
    }
  }

  const retry = () => {
    bootRef.current = null
    boot2DRef.current = null
    fittedRouteRef.current = ''
    setMapError(null)
    setReady3D(false)
    setReady2D(false)
    setReloadToken((value) => value + 1)
  }

  const changeMapMode = (mode: MapMode) => {
    if (mode === mapMode) return
    if (isVWorld2DMapMode(mode) !== vworld2DActive) fittedRouteRef.current = ''
    setMapError(null)
    setMapHover(null)
    setPinnedMapHover(null)
    onMapModeChange(mode)
  }

  return (
    <div
      ref={mapRootRef}
      className={`map-view ${highContrast ? 'high-contrast-mode' : ''}`}
      data-map-provider={mapProviderForMode(mapMode)}
      data-track-scope={
        visibleTrackIds
          ? trackOrder.filter((trackId) => visibleTrackIds.has(trackId)).join(',') || 'none'
          : displayAllTracks ? 'all' : effectiveTrackId ?? 'none'
      }
      data-track-layer-visible={hasVisibleTracks}
      data-route-feature-count={renderedRouteGeoJson.features.length}
      data-overlay-feature-count={overlayGeoJson.features.length}
      data-survey-feature-count={surveyGeoJson.features.length}
      data-survey-drawing={surveyDrawing}
      data-map-mode={mapMode}
    >
      {mapMode === '3d' && (
        <iframe
          key={`3d-${reloadToken}`}
          ref={iframeRef}
          className="map-container vworld-map-frame"
          src={`${VWORLD_IFRAME_URL}?reload=${reloadToken}`}
          title="VWorld WebGL 3D 지도"
        />
      )}
      {vworld2DActive && (
        <iframe
          key={`2d-${reloadToken}`}
          ref={iframe2DRef}
          className="map-container vworld-map-frame"
          src={`${VWORLD_2D_IFRAME_URL}?reload=${reloadToken}`}
          title={mapMode === 'satellite' ? 'VWorld 위성지도' : 'VWorld 2D 일반지도'}
        />
      )}
      <OverlayHoverTooltip
        hover={pinnedMapHover ?? mapHover}
        pinned={Boolean(pinnedMapHover)}
        onClose={() => {
          setPinnedMapHover(null)
          setMapHover(null)
        }}
        onDetails={(state) => {
          const current = overlayRef.current
          if (!current || !state.layerId) return
          current.selectFeature(
            { layerId: state.layerId, featureId: state.featureId },
            { navigate: false },
          )
          openOverlayFeatureDetails(current.datasetId, state)
        }}
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
        <div className="map-mode-switch" role="group" aria-label="지도 모드 선택">
          <button
            type="button"
            className={mapMode === '2d' ? 'active' : ''}
            aria-pressed={mapMode === '2d'}
            onClick={() => changeMapMode('2d')}
            title="VWorld 2D 일반 도로지도"
          >
            <MapIcon size={16} /> 2D
          </button>
          <button
            type="button"
            className={mapMode === 'satellite' ? 'active' : ''}
            aria-pressed={mapMode === 'satellite'}
            onClick={() => changeMapMode('satellite')}
            title="VWorld 공식 항공영상"
          >
            <ImageIcon size={16} /> 위성지도
          </button>
          <button
            type="button"
            className={mapMode === '3d' ? 'active' : ''}
            aria-pressed={mapMode === '3d'}
            onClick={() => changeMapMode('3d')}
            title="VWorld 3D 지형"
          >
            <Box size={16} /> 3D
          </button>
        </div>
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
        <span className="map-provider-badge">
          VWorld {mapMode === '2d' ? '2D 일반지도' : mapMode === 'satellite' ? '위성지도' : '3D'}
        </span>
        {hasVisibleTracks && (
          <span>
            <i
              className="legend-route"
              style={
                !visibleTrackIds && !displayAllTracks && effectiveTrackId
                  ? { background: trackColors.get(effectiveTrackId) ?? TRACK_COLORS[0] }
                  : undefined
              }
            />
            {visibleTrackIds
              ? `${visibleTrackIds.size.toLocaleString('ko-KR')}개 트랙`
              : displayAllTracks ? '전체 트랙' : '활성 트랙'}
          </span>
        )}
        {hasVisibleTracks && (
          <span>
            <i className="legend-frame" />
            MMS 프레임
          </span>
        )}
        {selectedFrame && visibleFrames.some((frame) => frame.id === selectedFrame.id) && (
          <span>
            <i className="legend-selected-frame" style={{ borderColor: MAP_SELECTED_FRAME_COLOR }} />
            선택 프레임
          </span>
        )}
        {overlay?.selectedFeature && (
          <span>
            <i
              className="legend-selected-feature"
              style={{ background: MAP_SELECTED_FEATURE_COLOR }}
            />
            선택 피처
          </span>
        )}
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
