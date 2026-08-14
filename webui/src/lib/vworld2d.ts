import type { Feature, FeatureCollection, Geometry, Position } from 'geojson'
import {
  MAP_SELECTED_FEATURE_COLOR,
  MAP_SELECTED_FRAME_COLOR,
} from './mapSelectionColors'
import type { CameraTarget } from './vworld'

export const VWORLD_2D_IFRAME_URL = '/vworld-2d-map.html'
export const VWORLD_2D_CONTAINER_ID = 'vmap'
export const VWORLD_2D_BASE_WMTS_URL =
  'https://api.vworld.kr/req/wmts/1.0.0/EE4D1CA9-BEA1-3AFF-AE81-DE0B92A0E352/Base/{z}/{y}/{x}.png'
export const VWORLD_2D_SATELLITE_WMTS_URL =
  'https://api.vworld.kr/req/wmts/1.0.0/EE4D1CA9-BEA1-3AFF-AE81-DE0B92A0E352/Satellite/{z}/{y}/{x}.jpeg'

export type VWorld2DBaseMap = 'base' | 'satellite'

type VWorld2DMapClickHandler = (event: { pixel?: unknown; coordinate?: unknown }) => void

interface VWorld2DFeature {
  get(key: string): unknown
  setStyle?(style: unknown): void
}

interface VWorld2DVectorSource {
  addFeatures(features: VWorld2DFeature[]): void
  clear(fast?: boolean): void
}

interface VWorld2DVectorLayer {
  setMap?(map: VWorld2DMap | null): void
}

interface VWorld2DTileLayer {
  setSource(source: VWorld2DXYZSource): void
}

interface VWorld2DXYZSource {}

interface VWorld2DView {
  fit(extent: unknown, size: unknown, options: Record<string, unknown>): void
  getZoom?(): number | undefined
  setCenter(center: unknown): void
  setZoom(zoom: number): void
}

export interface VWorld2DMap {
  /**
   * Compatibility state consumed by VWorld's patched OpenLayers 3 wheel
   * handler. Raw `ol.Map` does not create this property itself.
   */
  options?: {
    basemapType?: 'graphic' | 'photo'
    [key: string]: unknown
  }
  addLayer(layer: VWorld2DVectorLayer): void
  removeLayer(layer: VWorld2DVectorLayer): void
  getView(): VWorld2DView
  getSize(): unknown
  on(type: 'singleclick' | 'pointermove', handler: VWorld2DMapClickHandler): unknown
  un(type: 'singleclick' | 'pointermove', handler: VWorld2DMapClickHandler): void
  forEachFeatureAtPixel(
    pixel: unknown,
    callback: (feature: VWorld2DFeature) => unknown,
  ): unknown
  updateSize(): void
  setTarget?(target: null): void
}

interface VWorld2DStyleOptions {
  geometry?: unknown
  image?: unknown
  stroke?: unknown
  fill?: unknown
  zIndex?: number
}

interface OpenLayersNamespace {
  Map: new (...args: unknown[]) => VWorld2DMap
  Feature: new (options?: Record<string, unknown>) => VWorld2DFeature & {
    set(key: string, value: unknown): void
  }
  View: new (options: Record<string, unknown>) => VWorld2DView
  geom: {
    Point: new (coordinate: unknown) => unknown
    MultiPoint: new (coordinates: unknown) => unknown
    LineString: new (coordinates: unknown) => unknown
    MultiLineString: new (coordinates: unknown) => unknown
    Polygon: new (coordinates: unknown) => unknown
    MultiPolygon: new (coordinates: unknown) => unknown
    GeometryCollection: new (geometries: unknown[]) => unknown
  }
  layer: {
    Vector: new (options: Record<string, unknown>) => VWorld2DVectorLayer
    Tile: new (options: Record<string, unknown>) => VWorld2DTileLayer
  }
  source: {
    Vector: new (options?: Record<string, unknown>) => VWorld2DVectorSource
    XYZ: new (options: Record<string, unknown>) => VWorld2DXYZSource
  }
  style: {
    Style: new (options: VWorld2DStyleOptions) => unknown
    Stroke: new (options: Record<string, unknown>) => unknown
    Fill: new (options: Record<string, unknown>) => unknown
    Circle: new (options: Record<string, unknown>) => unknown
  }
  proj: {
    fromLonLat(coordinate: number[]): unknown
    toLonLat(coordinate: unknown): number[]
  }
  extent: {
    boundingExtent(coordinates: unknown[]): unknown
  }
  interaction: {
    defaults(options?: Record<string, unknown>): unknown
  }
}

interface VWorld2DFrameWindow extends Window {
  ol?: OpenLayersNamespace
  vworldIsValid?: string
  vworldErrMsg?: string
}

export interface VWorld2DRuntime {
  frameWindow: VWorld2DFrameWindow
  ol: OpenLayersNamespace
  map: VWorld2DMap
  baseLayer: VWorld2DTileLayer
  baseMap: VWorld2DBaseMap
}

export interface VWorld2DDataSource {
  source: VWorld2DVectorSource
  layer: VWorld2DVectorLayer
}

export interface VWorld2DSceneInput {
  route: FeatureCollection
  routeRange: FeatureCollection
  frames: FeatureCollection
  overlay: FeatureCollection
  onFrame: (frameId: string) => void
  onOverlay: (layerId: string, featureId: string) => void
}

export interface VWorld2DOverlayHoverTarget {
  layerId: string
  featureId: string
  properties: Record<string, unknown>
  pixel: [number, number]
}

export function assertVWorld2DSdk(frameWindow: Window): {
  frameWindow: VWorld2DFrameWindow
  ol: OpenLayersNamespace
} {
  const candidate = frameWindow as VWorld2DFrameWindow
  if (candidate.vworldIsValid === 'false') {
    const detail = candidate.vworldErrMsg?.trim()
    throw new Error(
      detail
        ? `VWorld 2D API 인증에 실패했습니다: ${detail}`
        : 'VWorld 2D API 인증에 실패했습니다. 개발키의 허용 URL을 확인해 주세요.',
    )
  }
  const ol = candidate.ol
  if (
    !ol?.Map ||
    !ol.View ||
    !ol.layer?.Tile ||
    !ol.source?.XYZ ||
    typeof ol.interaction?.defaults !== 'function'
  ) {
    throw new Error('VWorld 2D 일반지도 SDK를 불러오지 못했습니다.')
  }
  return {
    frameWindow: candidate,
    ol,
  }
}

export async function startVWorld2DMap(
  frameWindow: Window,
  mapId: string,
  initialTarget: CameraTarget,
  timeoutMs = 15_000,
  baseMap: VWorld2DBaseMap = 'base',
): Promise<VWorld2DRuntime> {
  const startedAt = Date.now()
  let sdk: ReturnType<typeof assertVWorld2DSdk> | null = null
  while (!sdk) {
    try {
      sdk = assertVWorld2DSdk(frameWindow)
    } catch (reason) {
      if ((frameWindow as VWorld2DFrameWindow).vworldIsValid === 'false') throw reason
      if (Date.now() - startedAt >= timeoutMs) throw reason
      await new Promise<void>((resolve) => frameWindow.setTimeout(resolve, 50))
    }
  }

  const { ol } = sdk
  const center = ol.proj.fromLonLat([initialTarget.lon, initialTarget.lat])
  const view = new ol.View({
    center,
    zoom: heightToZoom(initialTarget.height),
    minZoom: 6,
    maxZoom: 19,
  })
  // vw.ol3.Map prepends a visible OSM layer even when useOSM is false. Build
  // the OpenLayers map with one official VWorld Base layer so no fallback tile
  // provider can render or issue network requests.
  const baseSource = createVWorld2DBaseSource(ol, baseMap)
  const baseLayer = new ol.layer.Tile({ source: baseSource })
  const map = new ol.Map({
    target: mapId,
    layers: [baseLayer],
    view,
    controls: [],
    // VWorld ships an OpenLayers build whose implicit interaction defaults can
    // vary between loader versions. Declare wheel zoom explicitly and do not
    // gate it on iframe focus, so both Base and Satellite respond while the
    // pointer is over the map.
    interactions: ol.interaction.defaults({
      mouseWheelZoom: true,
      onFocusOnly: false,
    }),
    logo: false,
  })
  setVWorld2DWheelCompatibility(map, baseMap)
  map.updateSize()
  return { frameWindow: sdk.frameWindow, ol, map, baseLayer, baseMap }
}

/**
 * Swap only the official VWorld raster source. The OpenLayers map, view and
 * vector layers stay mounted so changing 2D imagery cannot reset the camera
 * or temporarily remove route/SHP overlays.
 */
export function setVWorld2DBaseMap(
  runtime: VWorld2DRuntime,
  baseMap: VWorld2DBaseMap,
): boolean {
  if (runtime.baseMap === baseMap) return false
  runtime.baseLayer.setSource(createVWorld2DBaseSource(runtime.ol, baseMap))
  setVWorld2DWheelCompatibility(runtime.map, baseMap)
  runtime.baseMap = baseMap
  return true
}

function setVWorld2DWheelCompatibility(
  map: VWorld2DMap,
  baseMap: VWorld2DBaseMap,
): void {
  // The VWorld loader patches MouseWheelZoom.handleEvent and reads
  // map.options.basemapType for native `wheel` events. Its runtime enum values
  // are `graphic` and `photo`; leaving options undefined makes wheel zoom throw
  // on a raw ol.Map before the view can change.
  map.options = {
    ...(map.options ?? {}),
    basemapType: baseMap === 'satellite' ? 'photo' : 'graphic',
  }
}

function createVWorld2DBaseSource(
  ol: OpenLayersNamespace,
  baseMap: VWorld2DBaseMap,
): VWorld2DXYZSource {
  return new ol.source.XYZ({
    url: baseMap === 'satellite'
      ? VWORLD_2D_SATELLITE_WMTS_URL
      : VWORLD_2D_BASE_WMTS_URL,
    crossOrigin: 'anonymous',
    minZoom: 6,
    maxZoom: 19,
  })
}

export function destroyVWorld2DMap(runtime: VWorld2DRuntime): void {
  try {
    runtime.map.setTarget?.(null)
  } catch {
    // The iframe can already be navigating or leaving the document.
  }
}

export function createVWorld2DDataSource(runtime: VWorld2DRuntime): VWorld2DDataSource {
  const source = new runtime.ol.source.Vector()
  const layer = new runtime.ol.layer.Vector({ source })
  runtime.map.addLayer(layer)
  return { source, layer }
}

export function removeVWorld2DDataSource(
  runtime: VWorld2DRuntime,
  dataSource: VWorld2DDataSource | null,
): void {
  if (!dataSource) return
  try {
    dataSource.source.clear(true)
    runtime.map.removeLayer(dataSource.layer)
  } catch {
    // The iframe can already be navigating or leaving the document.
  }
}

export function moveVWorld2DMap(
  runtime: VWorld2DRuntime,
  target: CameraTarget,
): void {
  const view = runtime.map.getView()
  // The VWorld 2D loader can serve older OpenLayers 3 builds. setCenter and
  // setZoom work across all supported builds, while View.animate does not.
  view.setCenter(runtime.ol.proj.fromLonLat([target.lon, target.lat]))
  view.setZoom(heightToZoom(target.height))
}

export function fitVWorld2DMap(
  runtime: VWorld2DRuntime,
  coordinates: ReadonlyArray<readonly [number, number]>,
): void {
  const projected = coordinates
    .filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat))
    .map(([lon, lat]) => runtime.ol.proj.fromLonLat([lon, lat]))
  if (projected.length === 0) return
  if (projected.length === 1) {
    runtime.map.getView().setCenter(projected[0])
    runtime.map.getView().setZoom(17)
    return
  }
  runtime.map.getView().fit(runtime.ol.extent.boundingExtent(projected), runtime.map.getSize(), {
    padding: [72, 72, 72, 72],
    maxZoom: 18,
  })
}

export function renderVWorld2DCollection(
  runtime: VWorld2DRuntime,
  dataSource: VWorld2DDataSource,
  collection: FeatureCollection,
  kind: 'route' | 'route-range' | 'frame' | 'overlay',
): void {
  const features = featuresForCollection(runtime, collection, kind)
  dataSource.source.clear(true)
  dataSource.source.addFeatures(features)
}

export function renderVWorld2DScene(
  runtime: VWorld2DRuntime,
  dataSource: VWorld2DDataSource,
  input: VWorld2DSceneInput,
): void {
  const features = [
    ...featuresForCollection(runtime, input.route, 'route'),
    ...featuresForCollection(runtime, input.routeRange, 'route-range'),
    ...featuresForCollection(runtime, input.frames, 'frame'),
    ...featuresForCollection(runtime, input.overlay, 'overlay'),
  ]
  dataSource.source.clear(true)
  dataSource.source.addFeatures(features)
}

export function handleVWorld2DClick(
  runtime: VWorld2DRuntime,
  event: { pixel?: unknown; coordinate?: unknown },
  input: Pick<VWorld2DSceneInput, 'onFrame' | 'onOverlay'>,
  onCoordinate?: (coordinate: [number, number, number]) => void,
): void {
  let handled = false
  if (event.pixel !== undefined) {
    runtime.map.forEachFeatureAtPixel(event.pixel, (feature) => {
      const frameId = feature.get('frame_id')
      if (typeof frameId === 'string' && frameId) {
        input.onFrame(frameId)
        handled = true
        return feature
      }
      const layerId = feature.get('overlay_layer_id')
      const featureId = feature.get('overlay_feature_id')
      if (typeof layerId === 'string' && typeof featureId === 'string' && layerId && featureId) {
        input.onOverlay(layerId, featureId)
        handled = true
        return feature
      }
      // Returning undefined keeps OpenLayers walking lower layers.  Saved
      // field-survey and route lines are visual guides, not hit targets.
      return undefined
    })
  }
  if (handled) return
  if (event.coordinate !== undefined && onCoordinate) {
    const [lon, lat] = runtime.ol.proj.toLonLat(event.coordinate)
    if (Number.isFinite(lon) && Number.isFinite(lat)) onCoordinate([lon, lat, 0])
  }
}

export function vworld2DOverlayHoverTarget(
  runtime: VWorld2DRuntime,
  event: { pixel?: unknown },
): VWorld2DOverlayHoverTarget | null {
  if (!Array.isArray(event.pixel) || event.pixel.length < 2) return null
  const pixelX = Number(event.pixel[0])
  const pixelY = Number(event.pixel[1])
  if (!Number.isFinite(pixelX) || !Number.isFinite(pixelY)) return null
  const picked = runtime.map.forEachFeatureAtPixel(event.pixel, (feature) => {
    const layerId = feature.get('overlay_layer_id')
    const featureId = feature.get('overlay_feature_id')
    const properties = feature.get('overlay_properties')
    return typeof layerId === 'string' &&
      typeof featureId === 'string' &&
      layerId &&
      featureId &&
      properties &&
      typeof properties === 'object' &&
      !Array.isArray(properties)
      ? feature
      : undefined
  })
  if (!picked || typeof picked !== 'object' || !('get' in picked)) return null
  const feature = picked as VWorld2DFeature
  const layerId = feature.get('overlay_layer_id')
  const featureId = feature.get('overlay_feature_id')
  const properties = feature.get('overlay_properties')
  if (
    typeof layerId !== 'string' ||
    typeof featureId !== 'string' ||
    !layerId ||
    !featureId ||
    !properties ||
    typeof properties !== 'object' ||
    Array.isArray(properties)
  ) {
    return null
  }
  return {
    layerId,
    featureId,
    properties: properties as Record<string, unknown>,
    pixel: [pixelX, pixelY],
  }
}

function featuresForCollection(
  runtime: VWorld2DRuntime,
  collection: FeatureCollection,
  kind: 'route' | 'route-range' | 'frame' | 'overlay',
): VWorld2DFeature[] {
  return collection.features.flatMap((feature) => {
    if (!geometryCoordinatesAreValid(feature.geometry)) return []
    const geometry = toOpenLayersGeometry(runtime.ol, feature.geometry)
    if (!geometry) return []
    const properties = feature.properties ?? {}
    const result = new runtime.ol.Feature({ geometry })
    Object.entries(properties).forEach(([key, value]) => result.set(key, value))
    result.setStyle?.(styleForFeature(runtime.ol, feature, kind))
    if (kind === 'frame') result.set('frame_id', String(properties.id ?? feature.id ?? ''))
    if (kind === 'overlay') {
      result.set('overlay_layer_id', String(properties.__overlay_layer_id ?? ''))
      result.set('overlay_feature_id', String(properties.__overlay_feature_id ?? feature.id ?? ''))
      result.set('overlay_properties', properties)
    }
    return [result]
  })
}

function geometryCoordinatesAreValid(geometry: Geometry | null): boolean {
  if (!geometry) return false
  if (geometry.type === 'GeometryCollection') {
    return geometry.geometries.every(geometryCoordinatesAreValid)
  }
  const visit = (value: unknown): boolean => {
    if (!Array.isArray(value)) return false
    if (value.length >= 2 && typeof value[0] === 'number' && typeof value[1] === 'number') {
      const lon = value[0]
      const lat = value[1]
      return Number.isFinite(lon) && Number.isFinite(lat) && lon >= -180 && lon <= 180 && lat >= -90 && lat <= 90
    }
    return value.every(visit)
  }
  return visit(geometry.coordinates)
}

function styleForFeature(
  ol: OpenLayersNamespace,
  feature: Feature<Geometry>,
  kind: 'route' | 'route-range' | 'frame' | 'overlay',
): unknown {
  const properties = feature.properties ?? {}
  const selected = Number(properties.selected ?? properties.__overlay_selected ?? 0) === 1
  const color = String(properties.track_color ?? properties.__overlay_color ?? '#579cf2')
  const selectedOverlay = kind === 'overlay' && selected
  const displayColor = selectedOverlay ? MAP_SELECTED_FEATURE_COLOR : color
  if (feature.geometry?.type === 'Point' || feature.geometry?.type === 'MultiPoint') {
    const createStyle = (radius: number) => new ol.style.Style({
        image: new ol.style.Circle({
          radius,
          fill: new ol.style.Fill({
            color: selectedOverlay ? MAP_SELECTED_FEATURE_COLOR : selected ? '#07111f' : color,
          }),
          stroke: new ol.style.Stroke({
            color: selectedOverlay
              ? '#fff2ec'
              : selected
                ? MAP_SELECTED_FRAME_COLOR
                : '#ffffff',
            width: selected ? 2 : 1.5,
          }),
        }),
        zIndex: selected ? 30 : kind === 'frame' ? 20 : 25,
      })
    if (kind === 'frame' && selected) {
      const cache = new Map<number, unknown>()
      return (_renderedFeature: unknown, resolution: number) => {
        const radius = selectedFrameRadiusForResolution(resolution)
        const existing = cache.get(radius)
        if (existing) return existing
        const style = createStyle(radius)
        cache.set(radius, style)
        return style
      }
    }
    const radius = kind === 'frame' ? 4 : selected ? 9 : 6
    return createStyle(radius)
  }
  if (feature.geometry?.type === 'LineString' || feature.geometry?.type === 'MultiLineString') {
    return new ol.style.Style({
      stroke: new ol.style.Stroke({
        color: displayColor,
        width: kind === 'route-range' ? 6 : kind === 'route' ? 4 : selected ? 6 : 3,
      }),
      zIndex: kind === 'route-range' ? 12 : kind === 'route' ? 10 : selected ? 22 : 20,
    })
  }
  return new ol.style.Style({
    stroke: new ol.style.Stroke({
      color: selectedOverlay ? MAP_SELECTED_FEATURE_COLOR : color,
      width: selected ? 4 : 2,
    }),
    fill: new ol.style.Fill({ color: withAlpha(displayColor, selected ? 0.52 : 0.24) }),
    zIndex: selected ? 22 : 20,
  })
}

export function selectedFrameRadiusForResolution(resolution: number): number {
  if (!Number.isFinite(resolution) || resolution <= 0) return 5
  const zoom = Math.log2(156_543.033_928_040_97 / resolution)
  if (zoom < 12) return 3
  if (zoom < 15) return 4
  if (zoom < 18) return 5
  return 6
}

function toOpenLayersGeometry(ol: OpenLayersNamespace, geometry: Geometry | null): unknown | null {
  if (!geometry) return null
  const project = (coordinate: Position) =>
    ol.proj.fromLonLat([Number(coordinate[0]), Number(coordinate[1])])
  switch (geometry.type) {
    case 'Point': return new ol.geom.Point(project(geometry.coordinates))
    case 'MultiPoint': return new ol.geom.MultiPoint(geometry.coordinates.map(project))
    case 'LineString': return new ol.geom.LineString(geometry.coordinates.map(project))
    case 'MultiLineString': return new ol.geom.MultiLineString(geometry.coordinates.map((line) => line.map(project)))
    case 'Polygon': return new ol.geom.Polygon(geometry.coordinates.map((ring) => ring.map(project)))
    case 'MultiPolygon': return new ol.geom.MultiPolygon(geometry.coordinates.map((polygon) => polygon.map((ring) => ring.map(project))))
    case 'GeometryCollection': {
      const children = geometry.geometries.map((child) => toOpenLayersGeometry(ol, child)).filter(isValue)
      return children.length > 0 ? new ol.geom.GeometryCollection(children) : null
    }
  }
}

function heightToZoom(height: number): number {
  if (!Number.isFinite(height) || height <= 0) return 16
  return Math.max(6, Math.min(19, 19 - Math.log2(Math.max(1, height / 120))))
}

function withAlpha(color: string, alpha: number): string {
  const normalized = /^#([0-9a-f]{6})$/i.exec(color)
  if (!normalized) return `rgba(255, 184, 77, ${alpha})`
  const value = normalized[1]
  return `rgba(${Number.parseInt(value.slice(0, 2), 16)}, ${Number.parseInt(value.slice(2, 4), 16)}, ${Number.parseInt(value.slice(4, 6), 16)}, ${alpha})`
}

function isValue<T>(value: T | null): value is T {
  return value !== null
}
