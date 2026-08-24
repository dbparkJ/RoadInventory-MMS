import type {
  Feature,
  FeatureCollection,
  GeoJsonProperties,
  Geometry,
  LineString,
  Point,
  Position,
} from 'geojson'
import {
  MAP_SELECTED_FEATURE_COLOR,
  MAP_SELECTED_FRAME_COLOR,
} from './mapSelectionColors'

export const VWORLD_IFRAME_URL = '/vworld-map.html'
export const VWORLD_CONTAINER_ID = 'vmap'
export const DEFAULT_MAP_CENTER = { lon: 126.978, lat: 37.5665, height: 7_500 }

type VWorldMapClickHandler = (
  windowPosition: unknown,
  ecefPosition: unknown,
  cartographic: {
    longitudeDD?: number
    latitudeDD?: number
    height?: number
  } | null,
  modelObject?: unknown,
) => void

interface VWorldEvent<THandler> {
  addEventListener(handler: THandler): void
  removeEventListener(handler: THandler): void
}

export interface VWorldMap {
  setOption(options: Record<string, unknown>): void
  setMapId(mapId: string): void
  setInitPosition(position: unknown): void
  setLogoVisible(visible: boolean): void
  setNavigationZoomVisible(visible: boolean): void
  start(): void
  moveTo(position: unknown): void
  updateSize(width?: number, height?: number): void
  clear(): void
  onClick: VWorldEvent<VWorldMapClickHandler>
}

interface VWorldNamespace {
  Map: new () => VWorldMap
  CoordZ: new (longitude: number, latitude: number, height: number) => unknown
  Direction: new (heading: number, tilt: number, roll: number) => unknown
  CameraPosition: new (coordinate: unknown, direction: unknown) => unknown
  ws3dInitCallBack?: () => void
}

interface VWorldEntity {
  id: string
}

export interface VWorldEntityCollection {
  add(options: Record<string, unknown>): VWorldEntity
  removeAll(): void
  suspendEvents?(): void
  resumeEvents?(): void
}

export interface VWorldCustomDataSource {
  entities: VWorldEntityCollection
}

interface VWorldDataSourceCollection {
  add(source: VWorldCustomDataSource): Promise<VWorldCustomDataSource> | VWorldCustomDataSource
  remove(source: VWorldCustomDataSource, destroy?: boolean): boolean
}

interface VWorldViewer {
  scene: {
    canvas: HTMLCanvasElement
    pick(position: unknown): unknown
    drillPick?(position: unknown, limit?: number): unknown
    pickPosition?(position: unknown): unknown
    globe?: {
      pick?(ray: unknown, scene: unknown): unknown
      ellipsoid?: {
        cartesianToCartographic?(cartesian: unknown): VWorldCartographic | null
      }
    }
  }
  camera?: {
    getPickRay?(position: unknown): unknown
    pickEllipsoid?(position: unknown, ellipsoid?: unknown): unknown
  }
  dataSources: VWorldDataSourceCollection
  forceResize?: () => void
}

interface VWorldCartographic {
  longitude?: number
  latitude?: number
}

interface VWorldColor {
  withAlpha(alpha: number): VWorldColor
}

interface CesiumNamespace {
  Cartesian3: {
    fromDegrees(longitude: number, latitude: number, height?: number): unknown
  }
  Color: {
    fromCssColorString(color: string): VWorldColor
  }
  CustomDataSource: new (name?: string) => VWorldCustomDataSource
  PolygonHierarchy: new (positions: unknown[], holes?: unknown[]) => unknown
  NearFarScalar?: new (
    near: number,
    nearValue: number,
    far: number,
    farValue: number,
  ) => unknown
  HeightReference: {
    CLAMP_TO_GROUND: unknown
  }
  ClassificationType?: {
    BOTH: unknown
  }
  Cartographic?: {
    fromCartesian(cartesian: unknown): VWorldCartographic | null
  }
  Ellipsoid?: {
    WGS84?: unknown
  }
  Math?: {
    toDegrees(radians: number): number
  }
}

interface VWorldFrameWindow extends Window {
  vw?: VWorldNamespace
  Cesium?: CesiumNamespace
  ws3d?: { viewer?: VWorldViewer }
  vworldIsValid?: string
  vworldErrMsg?: string
}

export interface VWorldRuntime {
  frameWindow: VWorldFrameWindow
  vw: VWorldNamespace
  Cesium: CesiumNamespace
  map: VWorldMap
  viewer: VWorldViewer
}

export interface CameraTarget {
  lon: number
  lat: number
  height: number
  heading?: number
  tilt?: number
}

export interface VWorldSceneInput {
  route: FeatureCollection
  routeRange: FeatureCollection
  frames: FeatureCollection
  overlay: FeatureCollection
  onFrame: (frameId: string) => void
  onOverlay: (layerId: string, featureId: string) => void
}

export interface VWorldOverlayHoverTarget {
  layerId: string
  featureId: string
  properties: Record<string, unknown>
}

export interface VWorldDistanceScale {
  near: number
  nearValue: number
  far: number
  farValue: number
}

export function selectedFrameDistanceScale(): VWorldDistanceScale {
  return { near: 60, nearValue: 0.9, far: 25_000, farValue: 0.3 }
}

/**
 * Resolve a canvas pointer to WGS84 without depending on the SDK click event.
 * Terrain picking is preferred; the ellipsoid fallback keeps the survey
 * preview responsive when depth picking is unavailable in VWorld.
 */
export function vworldCanvasWgs84Coordinate(
  runtime: VWorldRuntime,
  windowPosition: unknown,
): [number, number] | null {
  const { scene } = runtime.viewer
  let cartesian: unknown
  try {
    cartesian = scene.pickPosition?.(windowPosition)
  } catch {
    // Depth picking may be unsupported for the current browser/scene.
  }
  if (!cartesian) {
    try {
      const ray = runtime.viewer.camera?.getPickRay?.(windowPosition)
      if (ray) cartesian = scene.globe?.pick?.(ray, scene)
    } catch {
      // A ray can miss the globe near the horizon.
    }
  }
  if (!cartesian) {
    try {
      cartesian = runtime.viewer.camera?.pickEllipsoid?.(
        windowPosition,
        scene.globe?.ellipsoid ?? runtime.Cesium.Ellipsoid?.WGS84,
      )
    } catch {
      // Pointer is outside the rendered globe.
    }
  }
  if (!cartesian) return null

  let cartographic: VWorldCartographic | null | undefined
  try {
    cartographic = scene.globe?.ellipsoid?.cartesianToCartographic?.(cartesian)
      ?? runtime.Cesium.Cartographic?.fromCartesian(cartesian)
  } catch {
    return null
  }
  const longitude = Number(cartographic?.longitude)
  const latitude = Number(cartographic?.latitude)
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return null
  const toDegrees = runtime.Cesium.Math?.toDegrees ?? ((radians: number) => radians * 180 / Math.PI)
  const lon = toDegrees(longitude)
  const lat = toDegrees(latitude)
  return Number.isFinite(lon) && Number.isFinite(lat) ? [lon, lat] : null
}

export function assertVWorldSdk(frameWindow: Window): {
  frameWindow: VWorldFrameWindow
  vw: VWorldNamespace
  Cesium: CesiumNamespace
} {
  const candidate = frameWindow as VWorldFrameWindow
  if (candidate.vworldIsValid === 'false') {
    const detail = candidate.vworldErrMsg?.trim()
    throw new Error(
      detail
        ? `VWorld API 인증에 실패했습니다: ${detail}`
        : 'VWorld API 인증에 실패했습니다. 개발키의 허용 URL을 확인해 주세요.',
    )
  }
  if (!candidate.vw?.Map || !candidate.Cesium) {
    throw new Error('VWorld WebGL 3.0 SDK를 불러오지 못했습니다.')
  }
  return { frameWindow: candidate, vw: candidate.vw, Cesium: candidate.Cesium }
}

export async function startVWorldMap(
  frameWindow: Window,
  mapId: string,
  initialTarget: CameraTarget,
  timeoutMs = 15_000,
): Promise<VWorldRuntime> {
  const sdk = assertVWorldSdk(frameWindow)
  const { vw } = sdk
  const map = new vw.Map()
  const initPosition = createCameraPosition(vw, initialTarget)

  return new Promise<VWorldRuntime>((resolve, reject) => {
    let settled = false
    const timeout = sdk.frameWindow.setTimeout(() => {
      if (settled) return
      settled = true
      reject(new Error('VWorld 지도를 준비하는 데 시간이 초과되었습니다. 네트워크 상태를 확인해 주세요.'))
    }, timeoutMs)
    const previousCallback = vw.ws3dInitCallBack

    vw.ws3dInitCallBack = () => {
      try {
        previousCallback?.()
        const viewer = sdk.frameWindow.ws3d?.viewer
        if (!viewer) {
          throw new Error('VWorld 3D viewer가 초기화되지 않았습니다.')
        }
        if (!settled) {
          settled = true
          sdk.frameWindow.clearTimeout(timeout)
          resolve({ ...sdk, map, viewer })
        }
      } catch (reason) {
        if (!settled) {
          settled = true
          sdk.frameWindow.clearTimeout(timeout)
          reject(reason)
        }
      }
    }

    try {
      const options = {
        mapId,
        initPosition,
        logo: true,
        navigation: true,
      }
      map.setOption(options)
      map.setMapId(mapId)
      map.setInitPosition(initPosition)
      map.setLogoVisible(true)
      map.setNavigationZoomVisible(true)
      map.start()
    } catch (reason) {
      if (!settled) {
        settled = true
        sdk.frameWindow.clearTimeout(timeout)
        reject(reason)
      }
    }
  })
}

export async function createVWorldDataSource(
  runtime: VWorldRuntime,
  name = 'mms-workspace',
): Promise<VWorldCustomDataSource> {
  const source = new runtime.Cesium.CustomDataSource(name)
  await Promise.resolve(runtime.viewer.dataSources.add(source))
  return source
}

export function removeVWorldDataSource(
  runtime: VWorldRuntime,
  source: VWorldCustomDataSource | null,
): void {
  if (!source) return
  try {
    runtime.viewer.dataSources.remove(source, true)
  } catch {
    // Removing the iframe tears down the isolated viewer even if the SDK has
    // already begun its own shutdown sequence.
  }
}

export function createCameraPosition(vw: VWorldNamespace, target: CameraTarget): unknown {
  return new vw.CameraPosition(
    new vw.CoordZ(target.lon, target.lat, target.height),
    new vw.Direction(target.heading ?? 0, target.tilt ?? -70, 0),
  )
}

export function moveVWorldMap(runtime: VWorldRuntime, target: CameraTarget): void {
  runtime.map.moveTo(createCameraPosition(runtime.vw, target))
}

export function cameraTargetForSceneMode(
  target: CameraTarget,
  mode: '2d' | '3d',
): CameraTarget {
  if (mode === '2d') {
    return {
      ...target,
      height: Math.max(650, target.height),
      heading: 0,
      tilt: -90,
    }
  }
  return {
    ...target,
    heading: target.heading ?? 0,
    tilt: target.tilt === -90 || target.tilt === undefined ? -65 : target.tilt,
  }
}

export function setVWorldSceneMode(
  runtime: VWorldRuntime,
  mode: '2d' | '3d',
  target: CameraTarget,
): void {
  // VWorld WebGL 3.0 does not document Cesium's raw morphTo2D as a supported
  // runtime API. Calling it leaves VWorld terrain primitives without their
  // expected longitude metadata. A north-up nadir camera provides the 2D
  // inspection view while keeping the supported VWorld 3D scene intact.
  moveVWorldMap(runtime, cameraTargetForSceneMode(target, mode))
}

export function resizeVWorldMap(runtime: VWorldRuntime, width: number, height: number): void {
  runtime.map.updateSize(width, height)
  runtime.viewer.forceResize?.()
}

export function cameraTargetForCoordinates(
  coordinates: ReadonlyArray<readonly [number, number]>,
): CameraTarget {
  const valid = coordinates.filter(
    ([lon, lat]) =>
      Number.isFinite(lon) &&
      Number.isFinite(lat) &&
      lon >= -180 &&
      lon <= 180 &&
      lat >= -90 &&
      lat <= 90,
  )
  if (valid.length === 0) return { ...DEFAULT_MAP_CENTER, heading: 0, tilt: -70 }

  let minLon = valid[0][0]
  let maxLon = valid[0][0]
  let minLat = valid[0][1]
  let maxLat = valid[0][1]
  valid.forEach(([lon, lat]) => {
    minLon = Math.min(minLon, lon)
    maxLon = Math.max(maxLon, lon)
    minLat = Math.min(minLat, lat)
    maxLat = Math.max(maxLat, lat)
  })
  const lon = (minLon + maxLon) / 2
  const lat = (minLat + maxLat) / 2
  const northSouthMetres = (maxLat - minLat) * 111_320
  const eastWestMetres =
    (maxLon - minLon) * 111_320 * Math.max(0.2, Math.cos((lat * Math.PI) / 180))
  const span = Math.max(northSouthMetres, eastWestMetres)
  const height = Math.min(60_000, Math.max(800, span * 1.45 + 300))
  return { lon, lat, height, heading: 0, tilt: -70 }
}

export function renderVWorldScene(
  runtime: VWorldRuntime,
  source: VWorldCustomDataSource,
  input: VWorldSceneInput,
): ReadonlyMap<string, () => void> {
  const clickTargets = new Map<string, () => void>()
  replaceEntities(source.entities, () => {
    appendRoute(runtime, source.entities, input.route)
    appendRouteRange(runtime, source.entities, input.routeRange)
    appendFrames(runtime, source.entities, input.frames, input.onFrame, clickTargets)
    appendOverlay(runtime, source.entities, input.overlay, input.onOverlay, clickTargets)
  })
  return clickTargets
}

export function renderVWorldRoute(
  runtime: VWorldRuntime,
  source: VWorldCustomDataSource,
  route: FeatureCollection,
  onTrack?: (trackId: string) => void,
): ReadonlyMap<string, () => void> {
  const clickTargets = new Map<string, () => void>()
  replaceEntities(source.entities, () =>
    appendRoute(runtime, source.entities, route, onTrack, clickTargets),
  )
  return clickTargets
}

export function renderVWorldRouteRange(
  runtime: VWorldRuntime,
  source: VWorldCustomDataSource,
  routeRange: FeatureCollection,
): void {
  replaceEntities(source.entities, () =>
    appendRouteRange(runtime, source.entities, routeRange),
  )
}

export function renderVWorldFrames(
  runtime: VWorldRuntime,
  source: VWorldCustomDataSource,
  frames: FeatureCollection,
  onFrame: (frameId: string) => void,
): ReadonlyMap<string, () => void> {
  const clickTargets = new Map<string, () => void>()
  replaceEntities(source.entities, () =>
    appendFrames(runtime, source.entities, frames, onFrame, clickTargets),
  )
  return clickTargets
}

export function renderVWorldOverlay(
  runtime: VWorldRuntime,
  source: VWorldCustomDataSource,
  overlay: FeatureCollection,
  onOverlay: (layerId: string, featureId: string) => void,
  hoverTargets?: Map<string, VWorldOverlayHoverTarget>,
): ReadonlyMap<string, () => void> {
  const clickTargets = new Map<string, () => void>()
  replaceEntities(source.entities, () =>
    appendOverlay(runtime, source.entities, overlay, onOverlay, clickTargets, hoverTargets),
  )
  return clickTargets
}

function appendRoute(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  route: FeatureCollection,
  onTrack?: (trackId: string) => void,
  clickTargets?: Map<string, () => void>,
): void {
  route.features.forEach((feature, index) => {
    if (feature.geometry?.type !== 'LineString') return
    const color = propertyString(feature, 'track_color', '#579cf2')
    const selected = propertyNumber(feature, 'selected') === 1
    const trackId = propertyString(feature, 'track_id', '')
    const haloId = `route:${index}:halo`
    const routeId = `route:${index}`
    const haloAdded = addPolyline(runtime, entities, feature.geometry.coordinates, haloId, {
      color: '#07111f',
      alpha: selected ? 0.82 : 0.58,
      width: selected ? 12 : 8,
      zIndex: 0,
    })
    const routeAdded = addPolyline(runtime, entities, feature.geometry.coordinates, routeId, {
      color,
      alpha: 0.98,
      width: selected ? 7 : 4,
      zIndex: selected ? 3 : 1,
    })
    if (onTrack && clickTargets && trackId) {
      if (haloAdded) clickTargets.set(haloId, () => onTrack(trackId))
      if (routeAdded) clickTargets.set(routeId, () => onTrack(trackId))
    }
  })
}

function appendRouteRange(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  routeRange: FeatureCollection,
): void {
  routeRange.features.forEach((feature, index) => {
    if (feature.geometry?.type !== 'LineString') return
    const color = propertyString(feature, 'track_color', '#579cf2')
    addPolyline(runtime, entities, feature.geometry.coordinates, `route-range:${index}:halo`, {
      color: '#ffffff',
      alpha: 0.76,
      width: 10,
      zIndex: 2,
    })
    addPolyline(runtime, entities, feature.geometry.coordinates, `route-range:${index}`, {
      color,
      alpha: 1,
      width: 6,
      zIndex: 3,
    })
  })
}

function appendFrames(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  frames: FeatureCollection,
  onFrame: (frameId: string) => void,
  clickTargets: Map<string, () => void>,
): void {
  frames.features.forEach((feature, index) => {
    if (feature.geometry?.type !== 'Point') return
    const frameId = propertyString(feature, 'id', String(feature.id ?? ''))
    if (!frameId) return
    const selected = propertyNumber(feature, 'selected') === 1
    const inRange = propertyNumber(feature, 'in_range') === 1
    const trackColor = propertyString(feature, 'track_color', '#579cf2')
    const entityId = `frame:${index}`
    if (selected) {
      const haloId = `${entityId}:selected-halo`
      if (
        addPoint(runtime, entities, feature.geometry.coordinates, haloId, {
          color: MAP_SELECTED_FRAME_COLOR,
          size: 15,
          outlineColor: '#ffffff',
          outlineWidth: 2,
          distanceScale: selectedFrameDistanceScale(),
        })
      ) {
        clickTargets.set(haloId, () => onFrame(frameId))
      }
    }
    const added = addPoint(runtime, entities, feature.geometry.coordinates, entityId, {
      color: selected ? '#07111f' : trackColor,
      size: selected ? 8 : 7,
      outlineColor: selected ? MAP_SELECTED_FRAME_COLOR : inRange ? '#ffffff' : '#09261f',
      outlineWidth: selected ? 2 : inRange ? 3 : 1,
      distanceScale: selected ? selectedFrameDistanceScale() : undefined,
    })
    if (added) clickTargets.set(entityId, () => onFrame(frameId))
  })
}

function appendOverlay(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  overlay: FeatureCollection,
  onOverlay: (layerId: string, featureId: string) => void,
  clickTargets: Map<string, () => void>,
  hoverTargets?: Map<string, VWorldOverlayHoverTarget>,
): void {
  overlay.features.forEach((feature, featureIndex) => {
    const layerId = propertyString(feature, '__overlay_layer_id', '')
    const featureId = propertyString(
      feature,
      '__overlay_feature_id',
      String(feature.id ?? ''),
    )
    if (!feature.geometry || !layerId || !featureId) return
    const selected = propertyNumber(feature, '__overlay_selected') === 1
    const color = propertyString(feature, '__overlay_color', '#ffb84d')
    const hoverTarget: VWorldOverlayHoverTarget = {
      layerId,
      featureId,
      properties: { ...(feature.properties ?? {}) },
    }
    addOverlayGeometry(
      runtime,
      entities,
      feature.geometry,
      `overlay:${featureIndex}`,
      { color, selected },
      (entityId) => {
        clickTargets.set(entityId, () => onOverlay(layerId, featureId))
        hoverTargets?.set(entityId, hoverTarget)
      },
    )
  })
}

function replaceEntities(entities: VWorldEntityCollection, render: () => void): void {
  entities.suspendEvents?.()
  try {
    entities.removeAll()
    render()
  } finally {
    entities.resumeEvents?.()
  }
}

export function pickedEntityId(picked: unknown): string | null {
  if (!picked || typeof picked !== 'object' || !('id' in picked)) return null
  const candidate = (picked as { id?: unknown }).id
  if (typeof candidate === 'string') return candidate
  if (candidate && typeof candidate === 'object' && 'id' in candidate) {
    const id = (candidate as { id?: unknown }).id
    return typeof id === 'string' ? id : null
  }
  return null
}

/**
 * Return pickable entity ids from front to back.  Route and survey polylines
 * are deliberately non-interactive, so callers can continue through them to
 * the first frame/SHP entity instead of letting a decorative line swallow the
 * click or hover.
 */
export function pickedEntityIdsAtPosition(
  scene: {
    pick(position: unknown): unknown
    drillPick?(position: unknown, limit?: number): unknown
  },
  position: unknown,
  limit = 32,
): string[] {
  const drilled = scene.drillPick?.(position, limit)
  const picked = Array.isArray(drilled)
    ? drilled
    : [scene.pick(position)]
  const ids: string[] = []
  const seen = new Set<string>()
  picked.forEach((candidate) => {
    const id = pickedEntityId(candidate)
    if (!id || seen.has(id)) return
    seen.add(id)
    ids.push(id)
  })
  return ids
}

function addOverlayGeometry(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  geometry: Geometry,
  idPrefix: string,
  style: { color: string; selected: boolean },
  onEntity: (entityId: string) => void,
): void {
  const selectedColor = style.selected ? MAP_SELECTED_FEATURE_COLOR : style.color
  switch (geometry.type) {
    case 'Point': {
      const entityId = idPrefix
      if (
        addPoint(runtime, entities, geometry.coordinates, entityId, {
          color: selectedColor,
          size: style.selected ? 15 : 10,
          outlineColor: style.selected ? '#fff2ec' : '#ffffff',
          outlineWidth: style.selected ? 3 : 1,
        })
      ) {
        onEntity(entityId)
      }
      return
    }
    case 'MultiPoint':
      geometry.coordinates.forEach((coordinates, index) =>
        addOverlayGeometry(
          runtime,
          entities,
          { type: 'Point', coordinates } satisfies Point,
          `${idPrefix}:point:${index}`,
          style,
          onEntity,
        ),
      )
      return
    case 'LineString': {
      const entityId = idPrefix
      if (
        addPolyline(runtime, entities, geometry.coordinates, entityId, {
          color: selectedColor,
          alpha: 0.96,
          width: style.selected ? 6 : 3,
          zIndex: style.selected ? 12 : 10,
        })
      ) {
        onEntity(entityId)
      }
      return
    }
    case 'MultiLineString':
      geometry.coordinates.forEach((coordinates, index) =>
        addOverlayGeometry(
          runtime,
          entities,
          { type: 'LineString', coordinates } satisfies LineString,
          `${idPrefix}:line:${index}`,
          style,
          onEntity,
        ),
      )
      return
    case 'Polygon':
      addPolygon(runtime, entities, geometry.coordinates, idPrefix, style, onEntity)
      return
    case 'MultiPolygon':
      geometry.coordinates.forEach((coordinates, index) =>
        addPolygon(runtime, entities, coordinates, `${idPrefix}:polygon:${index}`, style, onEntity),
      )
      return
    case 'GeometryCollection':
      geometry.geometries.forEach((child, index) =>
        addOverlayGeometry(
          runtime,
          entities,
          child,
          `${idPrefix}:geometry:${index}`,
          style,
          onEntity,
        ),
      )
      return
  }
}

function addPoint(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  coordinates: Position,
  id: string,
  style: {
    color: string
    size: number
    outlineColor: string
    outlineWidth: number
    distanceScale?: VWorldDistanceScale
  },
): boolean {
  const position = toCartesian(runtime, coordinates)
  if (!position) return false
  entities.add({
    id,
    position,
    point: {
      pixelSize: style.size,
      color: cssColor(runtime, style.color),
      outlineColor: cssColor(runtime, style.outlineColor),
      outlineWidth: style.outlineWidth,
      heightReference: runtime.Cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
      ...(style.distanceScale && runtime.Cesium.NearFarScalar
        ? {
            scaleByDistance: new runtime.Cesium.NearFarScalar(
              style.distanceScale.near,
              style.distanceScale.nearValue,
              style.distanceScale.far,
              style.distanceScale.farValue,
            ),
          }
        : {}),
    },
  })
  return true
}

function addPolyline(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  coordinates: Position[],
  id: string,
  style: { color: string; alpha: number; width: number; zIndex: number },
): boolean {
  const positions = coordinates.map((coordinate) => toCartesian(runtime, coordinate)).filter(isValue)
  if (positions.length < 2) return false
  entities.add({
    id,
    polyline: {
      positions,
      width: style.width,
      material: cssColor(runtime, style.color, style.alpha),
      clampToGround: true,
      zIndex: style.zIndex,
    },
  })
  return true
}

function addPolygon(
  runtime: VWorldRuntime,
  entities: VWorldEntityCollection,
  rings: Position[][],
  id: string,
  style: { color: string; selected: boolean },
  onEntity: (entityId: string) => void,
): void {
  const selectedColor = style.selected ? MAP_SELECTED_FEATURE_COLOR : style.color
  const [outer, ...holes] = rings
  if (!outer) return
  const outerPositions = outer.map((coordinate) => toCartesian(runtime, coordinate)).filter(isValue)
  if (outerPositions.length < 3) return
  const holeHierarchies = holes.flatMap((ring) => {
    const positions = ring.map((coordinate) => toCartesian(runtime, coordinate)).filter(isValue)
    return positions.length >= 3 ? [new runtime.Cesium.PolygonHierarchy(positions)] : []
  })
  entities.add({
    id,
    polygon: {
      hierarchy: new runtime.Cesium.PolygonHierarchy(outerPositions, holeHierarchies),
      material: cssColor(runtime, selectedColor, style.selected ? 0.52 : 0.24),
      classificationType: runtime.Cesium.ClassificationType?.BOTH,
    },
  })
  onEntity(id)

  rings.forEach((ring, index) => {
    const outlineId = `${id}:outline:${index}`
    if (
      addPolyline(runtime, entities, ring, outlineId, {
        color: style.selected ? MAP_SELECTED_FEATURE_COLOR : style.color,
        alpha: 0.98,
        width: style.selected ? 4 : 2,
        zIndex: style.selected ? 12 : 10,
      })
    ) {
      onEntity(outlineId)
    }
  })
}

function toCartesian(runtime: VWorldRuntime, coordinate: Position): unknown | null {
  const lon = Number(coordinate[0])
  const lat = Number(coordinate[1])
  const height = Number(coordinate[2] ?? 0)
  if (
    !Number.isFinite(lon) ||
    !Number.isFinite(lat) ||
    lon < -180 ||
    lon > 180 ||
    lat < -90 ||
    lat > 90
  ) {
    return null
  }
  return runtime.Cesium.Cartesian3.fromDegrees(lon, lat, Number.isFinite(height) ? height : 0)
}

function cssColor(runtime: VWorldRuntime, value: string, alpha = 1): VWorldColor {
  let color: VWorldColor
  try {
    color = runtime.Cesium.Color.fromCssColorString(value)
  } catch {
    color = runtime.Cesium.Color.fromCssColorString('#ffb84d')
  }
  return alpha === 1 ? color : color.withAlpha(alpha)
}

function propertyString(
  feature: Feature<Geometry, GeoJsonProperties>,
  key: string,
  fallback: string,
): string {
  const value = feature.properties?.[key]
  return value === null || value === undefined ? fallback : String(value)
}

function propertyNumber(feature: Feature<Geometry, GeoJsonProperties>, key: string): number {
  return Number(feature.properties?.[key] ?? 0)
}

function isValue<T>(value: T | null): value is T {
  return value !== null
}
