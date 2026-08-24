import {
  Crosshair,
  Expand,
  LoaderCircle,
  Maximize2,
  MapPin,
  Minus,
  MousePointer2,
  Navigation,
  Plus,
  RefreshCcw,
  ScanLine,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import demoPanorama from '../assets/demo-panorama.svg'
import {
  openOverlayFeatureDetails,
  OverlayHoverTooltip,
  type OverlayHoverState,
} from '../components/OverlayHoverTooltip'
import { useOptionalOverlayWorkspace } from '../components/OverlayContext'
import {
  seamSafeBboxFromUvSamples,
  useOptionalManualObjectWorkspace,
} from '../components/ManualObjectContext'
import { api, ApiError } from '../lib/api'
import { createDemoPanoramaPoints, parseMmso } from '../lib/mmso'
import {
  panoramaUvToSpherePosition,
  projectDatasetPointToPanorama,
  type PanoramaHoverProjection,
} from '../lib/panoramaProjection'
import {
  directionalPanoramaTarget,
  type DirectionalPanoramaTarget,
} from '../lib/panoramaNavigation'
import {
  DEFAULT_POLE_BASE_MARKER_COLOR,
  DEFAULT_POLE_BASE_MARKER_SIZE_M,
  MAX_POLE_BASE_MARKER_SIZE_M,
  MIN_POLE_BASE_MARKER_SIZE_M,
  type PanoramaQuality,
} from '../lib/userSettings'
import type {
  Frame,
  PanoramaDetectionModel,
  PanoramaOverlayFeature,
  PanoramaPointPayload,
  PanoramaProjectionMetadata,
} from '../types'

export type { PanoramaQuality } from '../lib/userSettings'

export function panoramaForwardYaw(offsetDeg: number): number {
  // Leica equirectangular deliveries keep the vehicle forward direction at
  // the texture centre. Global GNSS heading belongs on the map, not in this
  // image-space reset, otherwise every frame turn makes the viewer look aside.
  return -180 + offsetDeg
}

export const PANORAMA_SCENE_CLICK_MAX_MOVEMENT_PX = 5
export const PANORAMA_WHEEL_LISTENER_OPTIONS: AddEventListenerOptions = Object.freeze({
  passive: false,
})

export function panoramaFovAfterWheel(currentFov: number, deltaY: number): number {
  const safeFov = Number.isFinite(currentFov) ? currentFov : 72
  if (!Number.isFinite(deltaY) || deltaY === 0) return Math.min(95, Math.max(28, safeFov))
  return Math.min(95, Math.max(28, safeFov + (deltaY > 0 ? 4 : -4)))
}

export function isPanoramaSceneClick(
  start: { x: number; y: number },
  end: { x: number; y: number },
  maximumMovementPx = PANORAMA_SCENE_CLICK_MAX_MOVEMENT_PX,
): boolean {
  return Math.hypot(end.x - start.x, end.y - start.y) <= maximumMovementPx
}

export function panoramaRayYaw(
  direction: { x: number; z: number },
  fallbackYaw: number,
): number {
  if (![direction.x, direction.z].every(Number.isFinite)) return fallbackYaw
  if (Math.hypot(direction.x, direction.z) < 1e-6) return fallbackYaw
  return Math.atan2(direction.z, direction.x) * 180 / Math.PI
}

export interface PanoramaSceneNavigationTarget {
  direction: -1 | 1
  target: DirectionalPanoramaTarget
}

/**
 * Resolve navigation for the ray under a scene click. Loaded track neighbors
 * are the only valid candidates. Index-only pagination cannot prove that an
 * unloaded frame lies within the clicked direction cone.
 */
export function panoramaSceneNavigationTarget(
  current: Frame | null,
  frames: Frame[],
  viewYaw: number,
  forwardYaw: number,
): PanoramaSceneNavigationTarget | null {
  if (!current) return null
  const target = directionalPanoramaTarget(current, frames, viewYaw, forwardYaw)
  if (target) return { direction: target.direction, target }
  return null
}

export const PANORAMA_PROGRESSIVE_PREVIEW_WIDTH = 768

export function panoramaProgressiveWidths(targetWidth: number): number[] {
  const safeTarget = Math.max(256, Math.min(8192, Math.round(targetWidth)))
  const previewWidth = Math.min(PANORAMA_PROGRESSIVE_PREVIEW_WIDTH, safeTarget)
  return previewWidth === safeTarget ? [safeTarget] : [previewWidth, safeTarget]
}

const PANORAMA_SCENE_CONTROL_SELECTOR = [
  'button',
  'a',
  'input',
  'select',
  'textarea',
  'summary',
  '[role="dialog"]',
  '.viewer-toolbar',
  '.viewer-data-card',
  '.viewer-loading',
  '.viewer-error',
  '.panorama-point-status',
  '.panorama-location-bar',
].join(',')

export function isPanoramaSceneControlTarget(target: EventTarget | null): boolean {
  // Avoid realm-specific instanceof checks: detached popups have their own
  // Element constructor even though closest() remains safe to call.
  const candidate = target as { closest?: (selector: string) => Element | null } | null
  return Boolean(candidate?.closest?.(PANORAMA_SCENE_CONTROL_SELECTOR))
}

export function panoramaRequestWidth(
  containerWidth: number,
  devicePixelRatio: number,
  quality: PanoramaQuality,
): number {
  const safeContainerWidth = containerWidth > 0 ? containerWidth : 1280
  const safePixelRatio = Math.min(2, Math.max(1, devicePixelRatio || 1))
  if (quality === 'ultra') return 8192
  // Fixed 4K derivatives stay cache-friendly and remain sharp when an overlay
  // is enlarged, detached to a second monitor, or switched to full screen.
  if (quality === 'high') return 4096
  const maximumWidth = 2048
  const minimumWidth = 960
  // Fast mode follows the current pane size to minimize transfer and decode time.
  const panoramaScale = 1.5
  return Math.min(
    maximumWidth,
    Math.max(minimumWidth, Math.round(safeContainerWidth * safePixelRatio * panoramaScale)),
  )
}

function createPanoramaPointTexture(
  ownerDocument: Document,
  payload: PanoramaPointPayload,
): THREE.CanvasTexture {
  const width = 2048
  const height = 1024
  const canvas = ownerDocument.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('파노라마 포인트 캔버스를 만들 수 없습니다.')
  const image = context.createImageData(width, height)
  const pixels = image.data

  for (let index = 0; index < payload.pointCount; index += 1) {
    const offset = index * 3
    const u = payload.coordinates[offset]
    const v = payload.coordinates[offset + 1]
    const distance = payload.coordinates[offset + 2]
    if (!Number.isFinite(u) || !Number.isFinite(v) || !Number.isFinite(distance)) continue
    const centerX = Math.round((((u % 1) + 1) % 1) * (width - 1))
    const centerY = Math.round(Math.min(1, Math.max(0, v)) * (height - 1))
    const radius = distance < 12 ? 2 : 1
    const red = payload.colors?.[offset] ?? 62
    const green = payload.colors?.[offset + 1] ?? 226
    const blue = payload.colors?.[offset + 2] ?? 189
    for (let dy = -radius; dy <= radius; dy += 1) {
      const y = centerY + dy
      if (y < 0 || y >= height) continue
      for (let dx = -radius; dx <= radius; dx += 1) {
        if (dx * dx + dy * dy > radius * radius + 1) continue
        const x = (centerX + dx + width) % width
        const pixelOffset = (y * width + x) * 4
        pixels[pixelOffset] = red
        pixels[pixelOffset + 1] = green
        pixels[pixelOffset + 2] = blue
        pixels[pixelOffset + 3] = 255
      }
    }
  }
  context.putImageData(image, 0, 0)
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.wrapS = THREE.RepeatWrapping
  texture.minFilter = THREE.LinearFilter
  texture.magFilter = THREE.LinearFilter
  texture.needsUpdate = true
  return texture
}

export interface RenderPanoramaOverlayPoint extends PanoramaOverlayFeature {
  layerId: string
  layerName: string
  color: string
  selected: boolean
  detectionBox?: PanoramaDetectionBox | null
}

export interface RenderPanoramaDetectionBox {
  sourceId: string
  observationId: string
  featureId?: string | number
  layerId?: string
  layerName: string
  properties: Record<string, unknown>
  color: string
  tooltipLayerColor?: string
  selected: boolean
  detectionBox: PanoramaDetectionBox
  modelKey?: string
}

interface PanoramaDetectionModelOption extends PanoramaDetectionModel {
  key: string
  name: string
}

const DETECTION_SOURCE_COLORS = ['#ffb84d', '#4dd9ff', '#ff6f91', '#7ee787', '#c59cff']

export function panoramaDetectionSourceColor(sourceId: string): string {
  let hash = 0
  for (let index = 0; index < sourceId.length; index += 1) {
    hash = ((hash * 31) + sourceId.charCodeAt(index)) >>> 0
  }
  return DETECTION_SOURCE_COLORS[hash % DETECTION_SOURCE_COLORS.length]
}

export interface PanoramaOverlayHit {
  layerId?: string
  layerName: string
  featureId: string | number
  properties: Record<string, unknown>
  color?: string
}

export function panoramaDetectionPointRadius(selected: boolean, depth: number): number {
  if (selected) return 3.5
  return depth < 20 ? 2 : 1.5
}

export interface PanoramaPoleBasePreview extends PanoramaHoverProjection {
  datasetPosition: readonly [number, number, number]
  color: string
  sizeM: number
}

function normalizedPoleBaseMarkerColor(value: string): string {
  const normalized = value.trim().toLowerCase()
  return /^#[0-9a-f]{6}$/.test(normalized)
    ? normalized
    : DEFAULT_POLE_BASE_MARKER_COLOR
}

/** Project only a proposal that belongs to the panorama currently on screen. */
export function panoramaPoleBasePreviewProjection({
  datasetPosition,
  proposalFrameId,
  currentFrameId,
  metadata,
  color = DEFAULT_POLE_BASE_MARKER_COLOR,
  sizeM = DEFAULT_POLE_BASE_MARKER_SIZE_M,
}: {
  datasetPosition: readonly [number, number, number] | null | undefined
  proposalFrameId: string | null | undefined
  currentFrameId: string | null | undefined
  metadata: PanoramaProjectionMetadata | null | undefined
  color?: string
  sizeM?: number
}): PanoramaPoleBasePreview | null {
  if (
    !datasetPosition
    || !proposalFrameId
    || !currentFrameId
    || !metadata
    || proposalFrameId !== currentFrameId
    || metadata.frame_id !== currentFrameId
  ) return null
  const projection = projectDatasetPointToPanorama(datasetPosition, metadata)
  if (!projection) return null
  const normalizedSize = Number.isFinite(sizeM)
    ? Math.min(MAX_POLE_BASE_MARKER_SIZE_M, Math.max(MIN_POLE_BASE_MARKER_SIZE_M, sizeM))
    : DEFAULT_POLE_BASE_MARKER_SIZE_M
  return {
    frameId: currentFrameId,
    ...projection,
    datasetPosition,
    color: normalizedPoleBaseMarkerColor(color),
    sizeM: normalizedSize,
  }
}

/** Convert a physical marker radius to equirectangular texture pixels. */
export function panoramaPoleBaseMarkerRadiusPx(
  sizeM: number,
  depth: number,
  textureWidth = 4096,
): number {
  const normalizedSize = Number.isFinite(sizeM)
    ? Math.min(MAX_POLE_BASE_MARKER_SIZE_M, Math.max(MIN_POLE_BASE_MARKER_SIZE_M, sizeM))
    : DEFAULT_POLE_BASE_MARKER_SIZE_M
  if (!Number.isFinite(depth) || depth <= 0 || !Number.isFinite(textureWidth) || textureWidth <= 0) {
    return 3
  }
  const angularRadius = Math.atan2(normalizedSize, depth)
  return Math.min(36, Math.max(3, angularRadius * textureWidth / (Math.PI * 2)))
}

export function panoramaDetectionStrokeWidth(selected: boolean): number {
  return selected ? 3 : 1.5
}

export function panoramaDetectionModelKey(sourceId: string, modelId?: string): string {
  return modelId?.trim() || sourceId
}

export function panoramaDetectionModels(
  models: PanoramaDetectionModel[] | undefined,
  boxes: RenderPanoramaDetectionBox[],
): PanoramaDetectionModelOption[] {
  const grouped = new Map<string, PanoramaDetectionModelOption>()
  if (models?.length) {
    models.forEach((model) => {
      const name = model.source_name?.trim() || model.source_id
      const key = panoramaDetectionModelKey(model.source_id, model.model_id)
      const existing = grouped.get(key)
      if (existing) existing.count += Math.max(0, model.count || 0)
      else grouped.set(key, { ...model, count: Math.max(0, model.count || 0), key, name })
    })
  } else {
    boxes.forEach((box) => {
      const modelName = String(propertyValue(box.properties, 'model_nm', 'model_name') ?? '')
        .trim()
      const name = modelName || box.layerName.replace(/^YOLO\s*[·ㆍ]\s*/u, '').trim() || box.sourceId
      const key = box.modelKey ?? panoramaDetectionModelKey(box.sourceId)
      const existing = grouped.get(key)
      if (existing) existing.count += 1
      else grouped.set(key, {
        source_id: box.sourceId,
        source_name: name,
        count: 1,
        key,
        name,
      })
    })
  }
  return [...grouped.values()].sort((left, right) => left.name.localeCompare(right.name, 'ko'))
}

export interface PanoramaDetectionBox {
  left: number
  top: number
  right: number
  bottom: number
  panoramaWidth: number
  panoramaHeight: number
  label: string
}

function propertyValue(
  properties: Record<string, unknown> | undefined,
  ...names: string[]
): unknown {
  if (!properties) return undefined
  const normalized = new Map(
    Object.entries(properties).map(([key, value]) => [key.toLocaleLowerCase('en-US'), value]),
  )
  for (const name of names) {
    const value = normalized.get(name.toLocaleLowerCase('en-US'))
    if (value !== undefined && value !== null && value !== '') return value
  }
  return undefined
}

function finiteProperty(
  properties: Record<string, unknown> | undefined,
  ...names: string[]
): number | null {
  const value = Number(propertyValue(properties, ...names))
  return Number.isFinite(value) ? value : null
}

function baseImageName(value: unknown): string {
  return String(value ?? '')
    .trim()
    .split(/[?#]/, 1)[0]
    .split(/[\\/]/)
    .at(-1)
    ?.normalize('NFC')
    .toLocaleLowerCase('en-US') ?? ''
}

function imageNamesMatch(source: unknown, current: unknown): boolean {
  const sourceName = baseImageName(source)
  const currentName = baseImageName(current)
  if (!sourceName || !currentName) return false
  if (sourceName === currentName) return true
  const sourceStem = sourceName.replace(/\.[^.]+$/, '')
  const currentStem = currentName.replace(/\.[^.]+$/, '')
  return sourceStem === currentStem
}

function finiteTuple(value: unknown): [number, number, number, number] | null {
  let candidate = value
  if (typeof candidate === 'string') {
    const text = candidate.trim()
    if (!text) return null
    try {
      candidate = JSON.parse(text)
    } catch {
      candidate = text.split(/[\s,;]+/)
    }
  }
  if (!Array.isArray(candidate) || candidate.length !== 4) return null
  const tuple = candidate.map(Number)
  return tuple.every(Number.isFinite)
    ? tuple as [number, number, number, number]
    : null
}

export function panoramaDetectionBox(
  properties: Record<string, unknown> | undefined,
  currentImageName?: string,
): PanoramaDetectionBox | null {
  const sourceImage = baseImageName(
    propertyValue(
      properties,
      'img_name',
      'image_name',
      'image',
      'filename',
      'image_path',
      'img_path',
      'source_image',
    ),
  )
  if (currentImageName && sourceImage && !imageNamesMatch(sourceImage, currentImageName)) return null
  const tuple = finiteTuple(propertyValue(properties, 'bbox_xyxy', 'bbox', 'box_xyxy'))
  const left = tuple?.[0] ?? finiteProperty(properties, 'bbox_l', 'bbox_left', 'x1', 'xmin')
  const top = tuple?.[1] ?? finiteProperty(properties, 'bbox_t', 'bbox_top', 'y1', 'ymin')
  const right = tuple?.[2] ?? finiteProperty(properties, 'bbox_r', 'bbox_right', 'x2', 'xmax')
  const bottom = tuple?.[3] ?? finiteProperty(properties, 'bbox_b', 'bbox_bottom', 'y2', 'ymax')
  const panoramaWidth = finiteProperty(
    properties,
    'pano_w',
    'panorama_width',
    'image_width',
    'img_width',
    'img_w',
    'orig_w',
    'width_px',
  )
  const panoramaHeight = finiteProperty(
    properties,
    'pano_h',
    'panorama_height',
    'image_height',
    'img_height',
    'img_h',
    'orig_h',
    'height_px',
  )
  if (
    left === null ||
    top === null ||
    right === null ||
    bottom === null ||
    panoramaWidth === null ||
    panoramaHeight === null ||
    panoramaWidth <= 0 ||
    panoramaHeight <= 0 ||
    right <= left ||
    bottom <= top
  ) {
    return null
  }
  const className = String(
    propertyValue(properties, 'class_nm', 'class_name', 'class', 'label') ?? '검출 객체',
  )
  const confidence = finiteProperty(properties, 'conf', 'confidence')
  const confidencePercent = confidence === null
    ? null
    : confidence <= 1
      ? confidence * 100
      : confidence
  return {
    left,
    top,
    right,
    bottom,
    panoramaWidth,
    panoramaHeight,
    label: confidencePercent === null
      ? className
      : `${className}\nconf ${Math.round(confidencePercent)}%`,
  }
}

function createPanoramaOverlayTexture(
  ownerDocument: Document,
  points: RenderPanoramaOverlayPoint[],
  detectionBoxes: RenderPanoramaDetectionBox[],
  poleBasePreview: PanoramaPoleBasePreview | null = null,
): THREE.CanvasTexture {
  // A 4K overlay texture keeps compact labels legible when the sphere is
  // enlarged, while the equirectangular seam remains repeat-wrapped.
  const width = 4096
  const height = 2048
  const canvas = ownerDocument.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new Error('SHP 오버레이 캔버스를 만들 수 없습니다.')

  const boxes = [
    ...points.filter((point) => point.detectionBox),
    ...detectionBoxes,
  ]
  boxes.forEach((point) => {
    const box = point.detectionBox
    if (!box) return
    const left = (box.left / box.panoramaWidth) * width
    const top = (box.top / box.panoramaHeight) * height
    const boxWidth = ((box.right - box.left) / box.panoramaWidth) * width
    const boxHeight = ((box.bottom - box.top) / box.panoramaHeight) * height
    if (boxWidth <= 0 || boxHeight <= 0 || boxWidth > width * 0.75) return
    context.lineWidth = panoramaDetectionStrokeWidth(point.selected)
    context.strokeStyle = point.selected ? '#ffffff' : point.color
    ;[-width, 0, width].forEach((shift) => {
      const x = left + shift
      if (x + boxWidth < 0 || x > width) return
      context.strokeRect(x, top, boxWidth, boxHeight)
    })
  })

  points.forEach((point) => {
    const x = (((point.u % 1) + 1) % 1) * width
    const y = Math.min(1, Math.max(0, point.v)) * height
    const radius = panoramaDetectionPointRadius(point.selected, point.depth)
    context.beginPath()
    context.arc(x, y, radius + 1.25, 0, Math.PI * 2)
    context.fillStyle = point.selected ? '#ffffff' : 'rgba(7, 17, 31, 0.9)'
    context.fill()
    context.beginPath()
    context.arc(x, y, radius, 0, Math.PI * 2)
    context.fillStyle = point.color
    context.fill()
    context.lineWidth = 1
    context.strokeStyle = '#ffffff'
    context.stroke()
  })

  if (poleBasePreview) {
    const x = (((poleBasePreview.u % 1) + 1) % 1) * width
    const y = Math.min(1, Math.max(0, poleBasePreview.v)) * height
    const radius = panoramaPoleBaseMarkerRadiusPx(
      poleBasePreview.sizeM,
      poleBasePreview.depth,
      width,
    )
    ;[-width, 0, width].forEach((shift) => {
      const shiftedX = x + shift
      if (shiftedX + radius + 3 < 0 || shiftedX - radius - 3 > width) return
      context.beginPath()
      context.arc(shiftedX, y, radius + 2.5, 0, Math.PI * 2)
      context.fillStyle = 'rgba(7, 17, 31, 0.92)'
      context.fill()
      context.beginPath()
      context.arc(shiftedX, y, radius, 0, Math.PI * 2)
      context.fillStyle = poleBasePreview.color
      context.fill()
      context.lineWidth = 1.5
      context.strokeStyle = '#ffffff'
      context.stroke()
    })
  }

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.wrapS = THREE.RepeatWrapping
  texture.minFilter = THREE.LinearFilter
  texture.magFilter = THREE.LinearFilter
  texture.needsUpdate = true
  return texture
}

function wrappedUVDistanceSquared(u: number, v: number, targetU: number, targetV: number): number {
  const directU = Math.abs(u - targetU)
  const wrappedU = Math.min(directU, 1 - Math.min(1, directU))
  return wrappedU * wrappedU + (v - targetV) * (v - targetV)
}

export interface PanoramaScreenProjection {
  viewportWidth: number
  viewportHeight: number
  verticalFovDeg: number
  yawDeg: number
  pitchDeg: number
}

export interface PanoramaScreenHitTest extends PanoramaScreenProjection {
  pointerX: number
  pointerY: number
  radiusPx?: number
}

/** Project an equirectangular point through the same perspective camera as the viewer. */
export function panoramaUvToScreenPosition(
  u: number,
  v: number,
  projection: PanoramaScreenProjection,
): { x: number; y: number } | null {
  const { viewportWidth, viewportHeight, verticalFovDeg, yawDeg, pitchDeg } = projection
  if (
    ![viewportWidth, viewportHeight, verticalFovDeg, yawDeg, pitchDeg].every(Number.isFinite)
    || viewportWidth <= 0
    || viewportHeight <= 0
    || verticalFovDeg <= 0
    || verticalFovDeg >= 180
  ) return null
  const position = panoramaUvToSpherePosition(u, v, 1)
  if (!position) return null

  const yaw = THREE.MathUtils.degToRad(yawDeg)
  const pitch = THREE.MathUtils.degToRad(pitchDeg)
  const cosPitch = Math.cos(pitch)
  const forward: [number, number, number] = [
    cosPitch * Math.cos(yaw),
    Math.sin(pitch),
    cosPitch * Math.sin(yaw),
  ]
  const right: [number, number, number] = [-Math.sin(yaw), 0, Math.cos(yaw)]
  const up: [number, number, number] = [
    right[1] * forward[2] - right[2] * forward[1],
    right[2] * forward[0] - right[0] * forward[2],
    right[0] * forward[1] - right[1] * forward[0],
  ]
  const dot = (axis: [number, number, number]) => (
    position[0] * axis[0] + position[1] * axis[1] + position[2] * axis[2]
  )
  const depth = dot(forward)
  if (!Number.isFinite(depth) || depth <= 1e-6) return null
  const verticalScale = Math.tan(THREE.MathUtils.degToRad(verticalFovDeg) / 2)
  const horizontalScale = verticalScale * viewportWidth / viewportHeight
  const ndcX = dot(right) / (depth * horizontalScale)
  const ndcY = dot(up) / (depth * verticalScale)
  if (
    ![ndcX, ndcY].every(Number.isFinite)
    || Math.abs(ndcX) > 1
    || Math.abs(ndcY) > 1
  ) return null
  return {
    x: (ndcX * 0.5 + 0.5) * viewportWidth,
    y: (-ndcY * 0.5 + 0.5) * viewportHeight,
  }
}

export function panoramaDetectionBoxContainsUv(
  box: PanoramaDetectionBox,
  u: number,
  v: number,
): boolean {
  const x = (((u % 1) + 1) % 1) * box.panoramaWidth
  const y = Math.min(1, Math.max(0, v)) * box.panoramaHeight
  if (y < box.top || y > box.bottom) return false
  return [-box.panoramaWidth, 0, box.panoramaWidth].some((shift) => {
    const shiftedX = x + shift
    return shiftedX >= box.left && shiftedX <= box.right
  })
}

export function panoramaOverlayAtUv(
  points: RenderPanoramaOverlayPoint[],
  u: number,
  v: number,
  detectionBoxes: RenderPanoramaDetectionBox[] = [],
  screenHit?: PanoramaScreenHitTest,
): PanoramaOverlayHit | null {
  let boxed: PanoramaOverlayHit | null = null
  let boxedArea = Number.POSITIVE_INFINITY
  detectionBoxes.forEach((observation) => {
    if (!panoramaDetectionBoxContainsUv(observation.detectionBox, u, v)) return
    const area = (
      (observation.detectionBox.right - observation.detectionBox.left)
      * (observation.detectionBox.bottom - observation.detectionBox.top)
    )
    if (area < boxedArea) {
      boxed = {
        ...(observation.featureId === undefined ? {} : { layerId: observation.layerId }),
        layerName: observation.layerName,
        featureId: observation.featureId ?? observation.observationId,
        properties: observation.properties,
        color: observation.tooltipLayerColor ?? observation.color,
      }
      boxedArea = area
    }
  })
  points.forEach((point) => {
    const box = point.detectionBox
    if (!box || !panoramaDetectionBoxContainsUv(box, u, v)) return
    const area = (box.right - box.left) * (box.bottom - box.top)
    if (area < boxedArea) {
      boxed = {
        layerId: point.layerId,
        layerName: point.layerName,
        featureId: point.feature_id,
        properties: point.properties ?? {},
        color: point.color,
      }
      boxedArea = area
    }
  })
  if (boxed) return boxed
  if (!screenHit) return null

  let nearest: PanoramaOverlayHit | null = null
  const hitRadius = Math.min(24, Math.max(1, screenHit.radiusPx ?? 9))
  let nearestDistance = hitRadius * hitRadius
  points.forEach((point) => {
    const position = panoramaUvToScreenPosition(point.u, point.v, screenHit)
    if (!position) return
    const distance = (
      (screenHit.pointerX - position.x) ** 2
      + (screenHit.pointerY - position.y) ** 2
    )
    if (distance <= nearestDistance) {
      nearestDistance = distance
      nearest = {
        layerId: point.layerId,
        layerName: point.layerName,
        featureId: point.feature_id,
        properties: point.properties ?? {},
        color: point.color,
      }
    }
  })
  return nearest
}

export function deduplicatePanoramaDetectionBoxes(
  boxes: RenderPanoramaDetectionBox[],
): RenderPanoramaDetectionBox[] {
  const seen = new Set<string>()
  return boxes.filter((box) => {
    const { left, top, right, bottom, panoramaWidth, panoramaHeight, label } = box.detectionBox
    // The same run/model result SHP can be opened more than once. Collapse
    // identical observations across those layers, while retaining coincident
    // detections produced by a different model or run.
    const key = [
      box.sourceId,
      box.observationId,
      left,
      top,
      right,
      bottom,
      panoramaWidth,
      panoramaHeight,
      label,
    ].join(':')
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function normalizedPropertyText(
  properties: Record<string, unknown> | undefined,
  ...names: string[]
): string {
  return String(propertyValue(properties, ...names) ?? '')
    .trim()
    .normalize('NFC')
    .toLocaleLowerCase('en-US')
}

function optionalPropertiesMatch(
  left: Record<string, unknown> | undefined,
  right: Record<string, unknown> | undefined,
  ...names: string[]
): boolean {
  const leftValue = normalizedPropertyText(left, ...names)
  const rightValue = normalizedPropertyText(right, ...names)
  return !leftValue || !rightValue || leftValue === rightValue
}

function panoramaDetectionBoxesMatch(
  left: PanoramaDetectionBox,
  right: PanoramaDetectionBox,
): boolean {
  const width = Math.max(left.panoramaWidth, right.panoramaWidth)
  const height = Math.max(left.panoramaHeight, right.panoramaHeight)
  if (
    Math.abs(left.panoramaWidth - right.panoramaWidth) > Math.max(1, width * 0.0001)
    || Math.abs(left.panoramaHeight - right.panoramaHeight) > Math.max(1, height * 0.0001)
  ) return false
  const tolerance = Math.max(1, width * 0.0001, height * 0.0001)
  if (
    Math.abs(left.top - right.top) > tolerance
    || Math.abs(left.bottom - right.bottom) > tolerance
  ) return false
  return [-width, 0, width].some((shift) => (
    Math.abs(left.left + shift - right.left) <= tolerance
    && Math.abs(left.right + shift - right.right) <= tolerance
  ))
}

/**
 * Attach a raw per-model observation to its representative visible SHP
 * feature. The raw box remains the sole visual rectangle, but hit-testing can
 * still open the layer's feature details. Coincident observations from another
 * model are not linked unless their identity/metadata is compatible.
 */
export function reconcilePanoramaDetectionBoxes(
  boxes: RenderPanoramaDetectionBox[],
  points: RenderPanoramaOverlayPoint[],
): RenderPanoramaDetectionBox[] {
  return boxes.map((box) => {
    const detectionId = normalizedPropertyText(box.properties, 'det_id', 'detection_id')
    const representative = points.find((point) => {
      const properties = point.properties
      const pointDetectionId = normalizedPropertyText(properties, 'det_id', 'detection_id')
      if (detectionId && pointDetectionId && detectionId !== pointDetectionId) return false
      if (!optionalPropertiesMatch(box.properties, properties, 'model_nm', 'model_name')) return false
      if (!optionalPropertiesMatch(box.properties, properties, 'img_name', 'image_name')) return false
      if (!optionalPropertiesMatch(box.properties, properties, 'class_nm', 'class_name')) return false

      const boxesMatch = point.detectionBox
        ? panoramaDetectionBoxesMatch(box.detectionBox, point.detectionBox)
        : false
      if (detectionId && pointDetectionId) {
        return point.detectionBox ? boxesMatch : true
      }
      return boxesMatch
    })
    if (!representative) return box
    return {
      ...box,
      featureId: representative.feature_id,
      layerId: representative.layerId,
      layerName: representative.layerName,
      tooltipLayerColor: representative.color,
      properties: {
        ...box.properties,
        ...(representative.properties ?? {}),
      },
    }
  })
}

export function nearestPanoramaPointIndex(
  u: number,
  v: number,
  coordinates: Float32Array,
  pointCount: number,
  maximumDistance = 0.03,
): number | null {
  let nearestIndex: number | null = null
  let nearestDistance = maximumDistance * maximumDistance
  for (let index = 0; index < pointCount; index += 1) {
    const offset = index * 3
    const candidateU = coordinates[offset]
    const candidateV = coordinates[offset + 1]
    if (!Number.isFinite(candidateU) || !Number.isFinite(candidateV)) continue
    const distance = wrappedUVDistanceSquared(u, v, candidateU, candidateV)
    if (distance <= nearestDistance) {
      nearestDistance = distance
      nearestIndex = index
    }
  }
  return nearestIndex
}

interface PanoramaRuntime {
  scene: THREE.Scene
  camera: THREE.PerspectiveCamera
  renderer: THREE.WebGLRenderer
  panoramaMesh: THREE.Mesh
  panoramaMaterial: THREE.MeshBasicMaterial
}

interface PanoramaNavigationPulse {
  x: number
  y: number
  targetFrameIndex: number
}

type PanoramaMediaStage = 'idle' | 'loading-preview' | 'preview' | 'enhancing' | 'ready'

export default function PanoramaView({
  datasetId,
  frame,
  demoMode,
  detectionRevisionKey = '',
  frames = [],
  onFrameChange,
  forwardOffsetDeg = 0,
  quality: controlledQuality,
  onQualityChange,
  pointOverlayEnabled: controlledPointOverlayEnabled,
  panoramaOpacity: controlledPanoramaOpacity,
  maxOverlayDistanceM = 45,
  poleBaseMarkerColor: requestedPoleBaseMarkerColor = DEFAULT_POLE_BASE_MARKER_COLOR,
  poleBaseMarkerSizeM: requestedPoleBaseMarkerSizeM = DEFAULT_POLE_BASE_MARKER_SIZE_M,
  linkedHoverPoint = null,
  onPointOverlayEnabledChange,
  onPanoramaOpacityChange,
}: {
  datasetId: string
  frame: Frame | null
  demoMode: boolean
  detectionRevisionKey?: string
  onPreviousFrame?: () => void
  onNextFrame?: () => void
  frames?: Frame[]
  onFrameChange?: (frame: Frame) => void
  hasPreviousFrame?: boolean
  hasNextFrame?: boolean
  forwardOffsetDeg?: number
  quality?: PanoramaQuality
  onQualityChange?: (quality: PanoramaQuality) => void
  pointOverlayEnabled?: boolean
  panoramaOpacity?: number
  maxOverlayDistanceM?: number
  poleBaseMarkerColor?: string
  poleBaseMarkerSizeM?: number
  linkedHoverPoint?: PanoramaHoverProjection | null
  onPointOverlayEnabledChange?: (enabled: boolean) => void
  onPanoramaOpacityChange?: (opacity: number) => void
}) {
  const overlay = useOptionalOverlayWorkspace()
  const manualObject = useOptionalManualObjectWorkspace()
  const stageRef = useRef<HTMLDivElement>(null)
  const linkedPointMarkerRef = useRef<HTMLDivElement>(null)
  const linkedHoverPointRef = useRef(linkedHoverPoint)
  const currentFrameIdRef = useRef(frame?.id)
  const currentMediaFrameKeyRef = useRef(frame ? `${datasetId}:${frame.id}` : null)
  const hoverFrameRef = useRef(0)
  const pendingHoverRef = useRef<{ x: number; y: number } | null>(null)
  const navigationFeedbackTimerRef = useRef<{ ownerWindow: Window; id: number } | null>(null)
  const navigationPulseTimerRef = useRef<{ ownerWindow: Window; id: number } | null>(null)
  const dragExceededThresholdRef = useRef(false)
  const [source, setSource] = useState<string | null>(demoMode ? demoPanorama : null)
  const [sourceFrameKey, setSourceFrameKey] = useState<string | null>(
    demoMode && frame ? `${datasetId}:${frame.id}` : null,
  )
  const [renderedFrameKey, setRenderedFrameKey] = useState<string | null>(
    demoMode && frame ? `${datasetId}:${frame.id}` : null,
  )
  const renderedFrameKeyRef = useRef(renderedFrameKey)
  const [loading, setLoading] = useState(!demoMode)
  const [mediaStage, setMediaStage] = useState<PanoramaMediaStage>(demoMode ? 'ready' : 'idle')
  const [error, setError] = useState<string | null>(null)
  const [fov, setFov] = useState(72)
  const [yaw, setYaw] = useState(() => panoramaForwardYaw(forwardOffsetDeg))
  const [pitch, setPitch] = useState(0)
  const [localQuality, setLocalQuality] = useState<PanoramaQuality>('high')
  const [localPointOverlayEnabled, setLocalPointOverlayEnabled] = useState(false)
  const [localPanoramaOpacity, setLocalPanoramaOpacity] = useState(0.65)
  const [pointPayload, setPointPayload] = useState<PanoramaPointPayload | null>(null)
  const [pointLoading, setPointLoading] = useState(false)
  const [pointIndexing, setPointIndexing] = useState(false)
  const [pointError, setPointError] = useState<string | null>(null)
  const [pointReloadKey, setPointReloadKey] = useState(0)
  const [overlayProjection, setOverlayProjection] = useState<RenderPanoramaOverlayPoint[]>([])
  const [detectionBoxes, setDetectionBoxes] = useState<RenderPanoramaDetectionBox[]>([])
  const [detectionModels, setDetectionModels] = useState<PanoramaDetectionModelOption[]>([])
  const [disabledDetectionModelKeys, setDisabledDetectionModelKeys] = useState<Set<string>>(
    () => new Set(),
  )
  const [detectionLoading, setDetectionLoading] = useState(false)
  const [detectionError, setDetectionError] = useState<string | null>(null)
  const [overlayLoading, setOverlayLoading] = useState(false)
  const [pickFeedback, setPickFeedback] = useState<string | null>(null)
  const [overlayHover, setOverlayHover] = useState<OverlayHoverState | null>(null)
  const [pinnedOverlayHover, setPinnedOverlayHover] = useState<OverlayHoverState | null>(null)
  const [navigationPulse, setNavigationPulse] = useState<PanoramaNavigationPulse | null>(null)
  const [sceneNavigationFeedback, setSceneNavigationFeedback] = useState<string | null>(null)
  const [frameAddress, setFrameAddress] = useState<string | null>(null)
  const [addressLoading, setAddressLoading] = useState(false)
  const [panoramaDimensions, setPanoramaDimensions] = useState({ width: 4096, height: 2048 })
  const [manualBboxDrag, setManualBboxDrag] = useState<{
    pointerId: number
    startX: number
    startY: number
    currentX: number
    currentY: number
  } | null>(null)
  const [manualBboxRect, setManualBboxRect] = useState<{
    left: number
    top: number
    width: number
    height: number
  } | null>(null)
  const [poleBaseProjectionMetadata, setPoleBaseProjectionMetadata] = useState<{
    datasetId: string
    metadata: PanoramaProjectionMetadata
  } | null>(null)
  const runtimeRef = useRef<PanoramaRuntime | null>(null)
  const panoramaTextureRef = useRef<THREE.Texture | null>(null)
  const lastMediaFrameKeyRef = useRef<string | null>(null)
  const quality = controlledQuality ?? localQuality
  const pointOverlayEnabled = controlledPointOverlayEnabled ?? localPointOverlayEnabled
  const panoramaOpacity = controlledPanoramaOpacity ?? localPanoramaOpacity
  const poleBaseMarkerColor = normalizedPoleBaseMarkerColor(requestedPoleBaseMarkerColor)
  const poleBaseMarkerSizeM = Number.isFinite(requestedPoleBaseMarkerSizeM)
    ? Math.min(
        MAX_POLE_BASE_MARKER_SIZE_M,
        Math.max(MIN_POLE_BASE_MARKER_SIZE_M, requestedPoleBaseMarkerSizeM),
      )
    : DEFAULT_POLE_BASE_MARKER_SIZE_M
  const poleBaseProposal = overlay?.poleBaseProposal
  const readyPoleBaseFrameId =
    poleBaseProposal?.status === 'ready'
    && poleBaseProposal.result.status !== 'failed'
    && poleBaseProposal.result.base_position
      ? poleBaseProposal.frameId
      : null
  const readyPoleBasePosition =
    poleBaseProposal?.status === 'ready'
    && poleBaseProposal.result.status !== 'failed'
      ? poleBaseProposal.result.base_position
      : null
  const manualProposalFrameId =
    manualObject?.proposalState.status === 'ready' || manualObject?.proposalState.status === 'committing'
      ? manualObject.proposalState.data.frameId
      : null
  const proposalProjectionFrameId = manualProposalFrameId ?? readyPoleBaseFrameId
  const activePoleBaseMetadata = poleBaseProjectionMetadata?.datasetId === datasetId
    ? poleBaseProjectionMetadata.metadata
    : null
  const poleBasePreview = useMemo(
    () => panoramaPoleBasePreviewProjection({
      datasetPosition: readyPoleBasePosition,
      proposalFrameId: readyPoleBaseFrameId,
      currentFrameId: frame?.id,
      metadata: activePoleBaseMetadata,
      color: poleBaseMarkerColor,
      sizeM: poleBaseMarkerSizeM,
    }),
    [
      activePoleBaseMetadata,
      frame?.id,
      poleBaseMarkerColor,
      poleBaseMarkerSizeM,
      readyPoleBaseFrameId,
      readyPoleBasePosition,
    ],
  )
  const manualObjectPreview = useMemo(
    () => panoramaPoleBasePreviewProjection({
      datasetPosition: manualObject?.proposalPosition ?? null,
      proposalFrameId: manualProposalFrameId,
      currentFrameId: frame?.id,
      metadata: activePoleBaseMetadata,
      color: '#ffb84d',
      sizeM: poleBaseMarkerSizeM,
    }),
    [activePoleBaseMetadata, frame?.id, manualObject?.proposalPosition, manualProposalFrameId, poleBaseMarkerSizeM],
  )
  const activeProposalPreview = manualObjectPreview ?? poleBasePreview
  const pointPayloadRequired = pointOverlayEnabled || Boolean(overlay?.pickMode)
  const visibleDetectionBoxes = useMemo(
    () => detectionBoxes.filter((box) => {
      const key = box.modelKey ?? panoramaDetectionModelKey(box.sourceId)
      return !disabledDetectionModelKeys.has(key)
    }),
    [detectionBoxes, disabledDetectionModelKeys],
  )
  const hasVisualOverlay = pointOverlayEnabled
    || overlayProjection.length > 0
    || visibleDetectionBoxes.length > 0
    || Boolean(activeProposalPreview)
  // SHP markers are already composited with a transparent texture. Only the
  // dense point-cloud overlay may dim the camera image at the user's request.
  const effectivePanoramaOpacity = pointOverlayEnabled ? panoramaOpacity : 1
  const panoramaOpacityRef = useRef(effectivePanoramaOpacity)
  panoramaOpacityRef.current = effectivePanoramaOpacity
  linkedHoverPointRef.current = linkedHoverPoint
  currentFrameIdRef.current = frame?.id
  currentMediaFrameKeyRef.current = frame ? `${datasetId}:${frame.id}` : null
  const viewRef = useRef({ fov, yaw, pitch })
  viewRef.current = { fov, yaw, pitch }
  const [dragStart, setDragStart] = useState<{
    pointerId: number
    x: number
    y: number
    yaw: number
    pitch: number
  } | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  const overlayActionsRef = useRef({
    pickMode: false,
    selectFeature: (
      _selection: { layerId: string; featureId: string | number } | null,
      _options?: { navigate?: boolean },
    ) => {},
    applyPickedCoordinate: async (
      _coordinates: [number, number, number?],
      _coordinateSpace: 'dataset',
    ) => {},
  })
  overlayActionsRef.current = {
    pickMode: overlay?.pickMode ?? false,
    selectFeature: overlay?.selectFeature ?? (() => {}),
    applyPickedCoordinate: overlay?.applyPickedCoordinate ?? (async () => {}),
  }
  const visibleOverlayLayers = useMemo(
    () =>
      (overlay?.layers ?? []).filter((layer) => overlay?.visibleLayerIds.has(layer.id)),
    [overlay?.layers, overlay?.visibleLayerIds],
  )
  const selectedOverlayKey = overlay?.selected
    ? `${overlay.selected.layerId}:${overlay.selected.featureId}`
    : ''
  const overlayLayerColor = overlay?.layerColor
  const overlayProjectionRevisionKey = visibleOverlayLayers
    .map((layer) => `${layer.id}:${overlay?.features[layer.id]?.dataset?.revision ?? layer.revision}`)
    .join('|')

  useEffect(() => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView
    if (hoverFrameRef.current && ownerWindow) ownerWindow.cancelAnimationFrame(hoverFrameRef.current)
    hoverFrameRef.current = 0
    pendingHoverRef.current = null
    setOverlayHover(null)
    setPinnedOverlayHover(null)
    setManualBboxDrag(null)
    setManualBboxRect(null)
    setIsDragging(false)
    dragExceededThresholdRef.current = false
    setError(null)
  }, [frame?.id])

  useEffect(() => {
    if (!manualObject?.bboxMode || manualObject.proposalState.status === 'drawing') {
      setManualBboxRect(null)
    }
  }, [manualObject?.bboxMode, manualObject?.proposalState.status])

  useEffect(() => {
    setFov(72)
    setYaw(panoramaForwardYaw(forwardOffsetDeg))
    setPitch(0)
  }, [datasetId, forwardOffsetDeg])

  useEffect(() => () => {
    const timer = navigationFeedbackTimerRef.current
    if (timer) timer.ownerWindow.clearTimeout(timer.id)
    const pulseTimer = navigationPulseTimerRef.current
    if (pulseTimer) pulseTimer.ownerWindow.clearTimeout(pulseTimer.id)
  }, [])

  useEffect(() => {
    const host = stageRef.current
    if (!host) return
    const handleWheel = (event: WheelEvent) => {
      if (!Number.isFinite(event.deltaY) || event.deltaY === 0) return
      event.preventDefault()
      setFov((current) => panoramaFovAfterWheel(current, event.deltaY))
    }
    // React delegates wheel events through a passive root listener in Chrome.
    // Register directly on the owning stage so preventDefault remains legal in
    // both the main document and detached panorama popup.
    host.addEventListener('wheel', handleWheel, PANORAMA_WHEEL_LISTENER_OPTIONS)
    return () => {
      host.removeEventListener('wheel', handleWheel, PANORAMA_WHEEL_LISTENER_OPTIONS)
    }
  }, [])

  useEffect(() => {
    setPoleBaseProjectionMetadata(null)
    if (!frame || demoMode || proposalProjectionFrameId !== frame.id) return
    const controller = new AbortController()
    const expectedFrameId = frame.id
    void api.panoramaProjectionMetadata(datasetId, expectedFrameId, controller.signal)
      .then((metadata) => {
        if (controller.signal.aborted || metadata.frame_id !== expectedFrameId) return
        setPoleBaseProjectionMetadata({ datasetId, metadata })
      })
      .catch(() => {
        // The proposal remains available in the 3D viewer when panorama
        // calibration metadata is unavailable; no approximate projection is drawn.
      })
    return () => controller.abort()
  }, [datasetId, demoMode, frame?.id, proposalProjectionFrameId])

  useEffect(() => {
    if (!frame?.coordinate || demoMode) {
      setFrameAddress(null)
      setAddressLoading(false)
      return
    }
    const controller = new AbortController()
    setFrameAddress(null)
    setAddressLoading(true)
    // Frame scrubbing can cross dozens of poses per second. Debounce before
    // hitting the real-time geocoder and abort any response from an old frame.
    const timer = window.setTimeout(() => {
      void api.frameAddress(datasetId, frame.id, controller.signal)
        .then((response) => {
          if (!controller.signal.aborted) setFrameAddress(response.address)
        })
        .catch(() => {
          // Address is supplemental context. Coordinates remain visible when
          // V-World is unavailable or has no address for this road position.
          if (!controller.signal.aborted) setFrameAddress(null)
        })
        .finally(() => {
          if (!controller.signal.aborted) setAddressLoading(false)
        })
    }, 300)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [datasetId, demoMode, frame?.coordinate?.lat, frame?.coordinate?.lon, frame?.id])

  useEffect(() => {
    // Visibility follows a model while the operator moves between frames, but
    // a different dataset starts with every model visible.
    setDisabledDetectionModelKeys(new Set())
    setDetectionModels([])
  }, [datasetId])

  useEffect(() => {
    const host = stageRef.current
    if (!host) return

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x07111f)
    const camera = new THREE.PerspectiveCamera(
      viewRef.current.fov,
      host.clientWidth / host.clientHeight,
      0.05,
      100,
    )
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' })
    } catch {
      setError('이 브라우저에서 WebGL 파노라마 뷰어를 시작할 수 없습니다.')
      return
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.6))
    renderer.setSize(host.clientWidth, host.clientHeight)
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.domElement.className = 'panorama-canvas'
    host.prepend(renderer.domElement)

    const panoramaGeometry = new THREE.SphereGeometry(10, 64, 40)
    panoramaGeometry.scale(-1, 1, 1)
    const panoramaMaterial = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: panoramaOpacityRef.current,
    })
    panoramaMaterial.visible = false
    const panoramaMesh = new THREE.Mesh(panoramaGeometry, panoramaMaterial)
    scene.add(panoramaMesh)
    runtimeRef.current = { scene, camera, renderer, panoramaMesh, panoramaMaterial }

    const ownerWindow = host.ownerDocument.defaultView ?? window
    const markerPosition = new THREE.Vector3()
    const cameraForward = new THREE.Vector3()
    let raf = 0
    const draw = () => {
      const phi = THREE.MathUtils.degToRad(90 - viewRef.current.pitch)
      const theta = THREE.MathUtils.degToRad(viewRef.current.yaw)
      camera.fov = viewRef.current.fov
      camera.updateProjectionMatrix()
      cameraForward.set(
        Math.sin(phi) * Math.cos(theta),
        Math.cos(phi),
        Math.sin(phi) * Math.sin(theta),
      )
      camera.lookAt(cameraForward.x * 10, cameraForward.y * 10, cameraForward.z * 10)
      renderer.render(scene, camera)
      const marker = linkedPointMarkerRef.current
      const linked = linkedHoverPointRef.current
      const spherePosition = linked && linked.frameId === currentFrameIdRef.current
        ? panoramaUvToSpherePosition(linked.u, linked.v)
        : null
      if (marker && spherePosition) {
        markerPosition.set(...spherePosition)
        const inFront = markerPosition.dot(cameraForward) > 0
        markerPosition.project(camera)
        const visible =
          inFront &&
          markerPosition.z >= -1 &&
          markerPosition.z <= 1 &&
          Math.abs(markerPosition.x) <= 1.05 &&
          Math.abs(markerPosition.y) <= 1.05
        marker.hidden = !visible
        if (visible) {
          marker.style.left = `${(markerPosition.x * 0.5 + 0.5) * renderer.domElement.clientWidth}px`
          marker.style.top = `${(-markerPosition.y * 0.5 + 0.5) * renderer.domElement.clientHeight}px`
        }
      } else if (marker) {
        marker.hidden = true
      }
      raf = ownerWindow.requestAnimationFrame(draw)
    }
    draw()

    const observer = new ResizeObserver(() => {
      const width = host.clientWidth
      const height = host.clientHeight
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      renderer.setSize(width, height)
    })
    observer.observe(host)
    return () => {
      ownerWindow.cancelAnimationFrame(raf)
      observer.disconnect()
      if (runtimeRef.current?.scene === scene) runtimeRef.current = null
      panoramaGeometry.dispose()
      panoramaMaterial.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [])

  useEffect(() => {
    if (!frame) {
      setSource(null)
      setSourceFrameKey(null)
      renderedFrameKeyRef.current = null
      setRenderedFrameKey(null)
      setLoading(false)
      setMediaStage('idle')
      lastMediaFrameKeyRef.current = null
      return
    }
    if (demoMode) {
      setSource(demoPanorama)
      setSourceFrameKey(`${datasetId}:${frame.id}`)
      setLoading(false)
      setMediaStage('ready')
      lastMediaFrameKeyRef.current = `${datasetId}:${frame.id}`
      return
    }

    const controller = new AbortController()
    let active = true
    let upgradeTimer: number | undefined
    let resolveUpgradeWait: (() => void) | undefined
    const objectUrls: string[] = []
    const ownerWindow = stageRef.current?.ownerDocument.defaultView ?? window
    const frameKey = `${datasetId}:${frame.id}`
    const isFrameTransition = lastMediaFrameKeyRef.current !== frameKey
    lastMediaFrameKeyRef.current = frameKey
    setLoading(true)
    setMediaStage(isFrameTransition ? 'loading-preview' : 'enhancing')
    setError(null)
    const containerWidth = stageRef.current?.clientWidth ?? 1280
    // Request a bounded viewport-sized derivative, never the multi-gigapixel source image.
    const targetWidth = panoramaRequestWidth(containerWidth, ownerWindow.devicePixelRatio, quality)
    const widths = isFrameTransition ? panoramaProgressiveWidths(targetWidth) : [targetWidth]
    const loadMedia = async () => {
      let hasUsablePreview = false
      for (let index = 0; index < widths.length; index += 1) {
        let result: Awaited<ReturnType<typeof api.panorama>>
        try {
          result = await api.panorama(datasetId, frame.id, widths[index], controller.signal)
        } catch (reason) {
          if (!active || controller.signal.aborted) return
          if (hasUsablePreview) {
            // A low-resolution texture is already usable. Keep it visible if
            // the optional high-resolution upgrade fails.
            setMediaStage('preview')
            return
          }
          throw reason
        }
        if (!active || controller.signal.aborted) return
        let nextSource: string
        if (result.kind === 'url') {
          nextSource = result.value
        } else {
          const nextObjectUrl = URL.createObjectURL(result.value)
          if (!active || controller.signal.aborted) {
            URL.revokeObjectURL(nextObjectUrl)
            return
          }
          objectUrls.push(nextObjectUrl)
          nextSource = nextObjectUrl
        }
        hasUsablePreview = true
        setSource(nextSource)
        setSourceFrameKey(frameKey)
        setLoading(false)
        if (index < widths.length - 1) {
          setMediaStage('preview')
          // Give React and the browser one paint opportunity to decode/show
          // the lightweight texture before starting the larger derivative.
          await new Promise<void>((resolve) => {
            resolveUpgradeWait = resolve
            upgradeTimer = ownerWindow.setTimeout(() => {
              resolveUpgradeWait = undefined
              resolve()
            }, 0)
          })
          if (!active || controller.signal.aborted) return
          setMediaStage('enhancing')
        } else {
          setMediaStage('ready')
        }
      }
    }
    void loadMedia()
      .catch((reason: unknown) => {
        if (active && !controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : '파노라마를 불러오지 못했습니다.')
          setMediaStage('idle')
        }
      })
      .finally(() => {
        if (active && !controller.signal.aborted) setLoading(false)
      })
    return () => {
      active = false
      controller.abort()
      if (upgradeTimer !== undefined) ownerWindow.clearTimeout(upgradeTimer)
      resolveUpgradeWait?.()
      objectUrls.forEach((objectUrl) => URL.revokeObjectURL(objectUrl))
    }
  }, [datasetId, demoMode, frame?.id, quality, reloadKey])

  useEffect(() => {
    const runtime = runtimeRef.current
    if (!runtime) return

    if (!source) {
      const previousTexture = panoramaTextureRef.current
      panoramaTextureRef.current = null
      renderedFrameKeyRef.current = null
      setRenderedFrameKey(null)
      runtime.panoramaMaterial.map = null
      runtime.panoramaMaterial.visible = false
      runtime.panoramaMaterial.needsUpdate = true
      previousTexture?.dispose()
      return
    }

    let active = true
    let disposed = false
    const disposeTexture = (texture: THREE.Texture) => {
      if (disposed) return
      disposed = true
      texture.dispose()
    }
    const texture = new THREE.TextureLoader().load(
      source,
      (readyTexture) => {
        if (
          !active
          || runtimeRef.current !== runtime
          || !sourceFrameKey
          || currentMediaFrameKeyRef.current !== sourceFrameKey
        ) {
          disposeTexture(readyTexture)
          return
        }
        readyTexture.colorSpace = THREE.SRGBColorSpace
        readyTexture.needsUpdate = true
        const image = readyTexture.image as
          | { naturalWidth?: number; naturalHeight?: number; width?: number; height?: number }
          | undefined
        const width = image?.naturalWidth ?? image?.width
        const height = image?.naturalHeight ?? image?.height
        if (width && height) setPanoramaDimensions({ width, height })
        const previousTexture = panoramaTextureRef.current
        panoramaTextureRef.current = readyTexture
        renderedFrameKeyRef.current = sourceFrameKey
        setRenderedFrameKey(sourceFrameKey)
        setError(null)
        runtime.panoramaMaterial.map = readyTexture
        runtime.panoramaMaterial.visible = true
        runtime.panoramaMaterial.needsUpdate = true
        if (previousTexture && previousTexture !== readyTexture) previousTexture.dispose()
      },
      undefined,
      () => {
        if (
          active
          && runtimeRef.current === runtime
          && sourceFrameKey
          && currentMediaFrameKeyRef.current === sourceFrameKey
        ) {
          if (panoramaTextureRef.current && renderedFrameKeyRef.current === sourceFrameKey) {
            setMediaStage('preview')
          }
          else setError('파노라마 텍스처를 디코딩하지 못했습니다.')
        }
      },
    )
    texture.colorSpace = THREE.SRGBColorSpace

    return () => {
      active = false
      if (panoramaTextureRef.current !== texture) disposeTexture(texture)
    }
  }, [source, sourceFrameKey])

  useEffect(() => () => {
    panoramaTextureRef.current?.dispose()
    panoramaTextureRef.current = null
  }, [])

  useEffect(() => {
    if (!frame || demoMode || !visibleOverlayLayers.length) {
      setOverlayProjection([])
      setOverlayLoading(false)
      return
    }
    const controller = new AbortController()
    setOverlayProjection([])
    setPickFeedback(null)
    setOverlayLoading(true)
    const loadProjection = async () => {
      const groups: RenderPanoramaOverlayPoint[][] = []
      const errors: unknown[] = []
      let nextIndex = 0
      const worker = async () => {
        while (!controller.signal.aborted) {
          const index = nextIndex
          nextIndex += 1
          const layer = visibleOverlayLayers[index]
          if (!layer) return
          try {
            const response = await api.panoramaOverlayProjection(
              datasetId,
              layer.id,
              frame.id,
              controller.signal,
              maxOverlayDistanceM,
            )
            const color = overlayLayerColor?.(layer.id) ?? '#ffb84d'
            groups[index] = response.items.map((item) => ({
              ...item,
              layerId: layer.id,
              layerName: layer.name,
              color,
              selected: false,
              detectionBox: panoramaDetectionBox(item.properties, frame.image_name),
            }))
          } catch (reason) {
            if (!controller.signal.aborted) errors.push(reason)
            groups[index] = []
          }
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(4, visibleOverlayLayers.length) }, () => worker()),
      )
      if (controller.signal.aborted) return
      setOverlayProjection(groups.flat())
      if (errors.length) {
        setPickFeedback(
          errors.length === visibleOverlayLayers.length
            ? errors[0] instanceof Error
              ? errors[0].message
              : '파노라마 SHP 위치를 불러오지 못했습니다.'
            : `일부 SHP 레이어(${errors.length}개)를 파노라마에 맞추지 못했습니다.`,
        )
      }
    }
    void loadProjection()
      .finally(() => {
        if (!controller.signal.aborted) setOverlayLoading(false)
      })
    return () => controller.abort()
  }, [
    datasetId,
    demoMode,
    frame,
    overlayLayerColor,
    overlayProjectionRevisionKey,
    maxOverlayDistanceM,
    visibleOverlayLayers,
  ])

  useEffect(() => {
    if (!frame || demoMode) {
      setDetectionBoxes([])
      setDetectionModels([])
      setDetectionLoading(false)
      setDetectionError(null)
      return
    }
    const controller = new AbortController()
    setDetectionBoxes([])
    setDetectionLoading(true)
    setDetectionError(null)
    void api.frameDetections(datasetId, frame.id, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return
        const boxes = response.items.flatMap((observation) => {
          const detectionBox = panoramaDetectionBox(observation.properties, frame.image_name)
          if (!detectionBox) return []
          return [{
            sourceId: observation.source_id,
            observationId: observation.observation_id,
            featureId: observation.feature_id,
            layerName: observation.source_name
              ? `YOLO · ${observation.source_name}`
              : 'YOLO 검출',
            properties: observation.properties,
            color: panoramaDetectionSourceColor(observation.model_id ?? observation.source_id),
            selected: false,
            detectionBox,
            modelKey: panoramaDetectionModelKey(observation.source_id, observation.model_id),
          } satisfies RenderPanoramaDetectionBox]
        })
        const deduplicatedBoxes = deduplicatePanoramaDetectionBoxes(boxes)
        setDetectionBoxes(deduplicatedBoxes)
        setDetectionModels(panoramaDetectionModels(response.models, deduplicatedBoxes))
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setDetectionModels([])
          setDetectionError(
            reason instanceof Error ? reason.message : 'YOLO 검출 박스를 불러오지 못했습니다.',
          )
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetectionLoading(false)
      })
    return () => controller.abort()
  }, [datasetId, demoMode, detectionRevisionKey, frame])

  const reconciledDetectionBoxes = useMemo(
    () => reconcilePanoramaDetectionBoxes(visibleDetectionBoxes, overlayProjection),
    [overlayProjection, visibleDetectionBoxes],
  )

  const renderedOverlayProjection = useMemo(
    () =>
      overlayProjection.map((point) => ({
        ...point,
        selected: `${point.layerId}:${point.feature_id}` === selectedOverlayKey,
        // The frame endpoint contains every raw model observation. Retain a
        // representative SHP bbox only as a fallback for external/legacy data.
        detectionBox: detectionModels.length || detectionBoxes.length ? null : point.detectionBox,
      })),
    [detectionBoxes.length, detectionModels.length, overlayProjection, selectedOverlayKey],
  )

  const renderedDetectionBoxes = useMemo(
    () => reconciledDetectionBoxes.map((box) => ({
      ...box,
      selected: box.layerId !== undefined && box.featureId !== undefined
        && `${box.layerId}:${box.featureId}` === selectedOverlayKey,
    })),
    [reconciledDetectionBoxes, selectedOverlayKey],
  )

  useEffect(() => {
    const runtime = runtimeRef.current
    const host = stageRef.current
    if (
      !runtime
      || !host
      || (!renderedOverlayProjection.length && !renderedDetectionBoxes.length && !activeProposalPreview)
    ) return
    const geometry = new THREE.SphereGeometry(9.92, 64, 40)
    geometry.scale(-1, 1, 1)
    const texture = createPanoramaOverlayTexture(
      host.ownerDocument,
      renderedOverlayProjection,
      renderedDetectionBoxes,
      activeProposalPreview,
    )
    const material = new THREE.MeshBasicMaterial({
      map: texture,
      transparent: true,
      opacity: 1,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
    })
    const mesh = new THREE.Mesh(geometry, material)
    mesh.renderOrder = 3
    runtime.scene.add(mesh)
    return () => {
      runtime.scene.remove(mesh)
      geometry.dispose()
      material.dispose()
      texture.dispose()
    }
  }, [activeProposalPreview, renderedDetectionBoxes, renderedOverlayProjection])

  useEffect(() => {
    if (!frame || !pointPayloadRequired) {
      setPointPayload(null)
      setPointLoading(false)
      setPointIndexing(false)
      setPointError(null)
      return
    }
    if (demoMode) {
      setPointPayload(createDemoPanoramaPoints())
      setPointLoading(false)
      setPointIndexing(false)
      setPointError(null)
      return
    }

    const controller = new AbortController()
    let retryTimer: number | undefined
    let attempts = 0
    setPointPayload(null)
    setPointLoading(true)
    setPointIndexing(false)
    setPointError(null)
    const load = async () => {
      try {
        const payload = parseMmso(
          await api.panoramaPoints(datasetId, frame.id, 30_000, 30, controller.signal),
        )
        if (!controller.signal.aborted) {
          setPointPayload(payload)
          setPointLoading(false)
          setPointIndexing(false)
        }
      } catch (reason) {
        if (controller.signal.aborted) return
        if (reason instanceof ApiError && reason.status === 202 && attempts < 8) {
          attempts += 1
          setPointIndexing(true)
          retryTimer = window.setTimeout(load, Math.min(8_000, attempts * 1_200))
          return
        }
        setPointError(
          reason instanceof Error ? reason.message : '파노라마 포인트를 불러오지 못했습니다.',
        )
        setPointLoading(false)
      }
    }
    void load()
    return () => {
      controller.abort()
      if (retryTimer) window.clearTimeout(retryTimer)
    }
  }, [datasetId, demoMode, frame, pointPayloadRequired, pointReloadKey])

  useEffect(() => {
    const runtime = runtimeRef.current
    const host = stageRef.current
    if (!runtime || !host || !pointPayload || !pointOverlayEnabled) return

    const overlayGeometry = new THREE.SphereGeometry(9.96, 64, 40)
    overlayGeometry.scale(-1, 1, 1)
    const overlayTexture = createPanoramaPointTexture(host.ownerDocument, pointPayload)
    const overlayMaterial = new THREE.MeshBasicMaterial({
      map: overlayTexture,
      transparent: true,
      opacity: 1,
      depthTest: false,
      depthWrite: false,
      toneMapped: false,
    })
    const overlay = new THREE.Mesh(overlayGeometry, overlayMaterial)
    overlay.renderOrder = 2
    runtime.scene.add(overlay)

    return () => {
      runtime.scene.remove(overlay)
      overlayGeometry.dispose()
      overlayMaterial.dispose()
      overlayTexture.dispose()
    }
  }, [pointOverlayEnabled, pointPayload])

  useEffect(() => {
    if (!runtimeRef.current) return
    runtimeRef.current.panoramaMaterial.opacity = effectivePanoramaOpacity
    runtimeRef.current.panoramaMaterial.needsUpdate = true
  }, [effectivePanoramaOpacity])

  const changeZoom = (delta: number) => {
    setFov((current) => Math.min(95, Math.max(28, current + delta)))
  }

  const toggleFullscreen = async () => {
    if (!stageRef.current) return
    const ownerDocument = stageRef.current.ownerDocument
    if (ownerDocument.fullscreenElement) await ownerDocument.exitFullscreen()
    else await stageRef.current.requestFullscreen()
  }

  const forwardYaw = panoramaForwardYaw(forwardOffsetDeg)

  const showSceneNavigationFeedback = (message: string) => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView ?? window
    const previousTimer = navigationFeedbackTimerRef.current
    if (previousTimer) previousTimer.ownerWindow.clearTimeout(previousTimer.id)
    setSceneNavigationFeedback(message)
    navigationFeedbackTimerRef.current = {
      ownerWindow,
      id: ownerWindow.setTimeout(() => {
        setSceneNavigationFeedback(null)
        navigationFeedbackTimerRef.current = null
      }, 1_200),
    }
  }

  const showNavigationPulse = (x: number, y: number, targetFrameIndex: number) => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView ?? window
    const previousTimer = navigationPulseTimerRef.current
    if (previousTimer) previousTimer.ownerWindow.clearTimeout(previousTimer.id)
    setNavigationPulse({ x, y, targetFrameIndex })
    navigationPulseTimerRef.current = {
      ownerWindow,
      id: ownerWindow.setTimeout(() => {
        setNavigationPulse(null)
        navigationPulseTimerRef.current = null
      }, 700),
    }
  }

  const moveToSceneNavigation = (
    navigation: PanoramaSceneNavigationTarget,
    pointer: { x: number; y: number },
  ): boolean => {
    if (!onFrameChange) return false
    showNavigationPulse(pointer.x, pointer.y, navigation.target.frame.index)
    showSceneNavigationFeedback(`Frame ${navigation.target.frame.index + 1}로 이동합니다.`)
    onFrameChange(navigation.target.frame)
    return true
  }

  const changeQuality = (nextQuality: PanoramaQuality) => {
    if (controlledQuality === undefined) setLocalQuality(nextQuality)
    onQualityChange?.(nextQuality)
  }

  const changePointOverlay = (enabled: boolean) => {
    if (controlledPointOverlayEnabled === undefined) setLocalPointOverlayEnabled(enabled)
    onPointOverlayEnabledChange?.(enabled)
  }

  const changeDetectionModelVisibility = (modelKey: string, enabled: boolean) => {
    setDisabledDetectionModelKeys((current) => {
      const next = new Set(current)
      if (enabled) next.delete(modelKey)
      else next.add(modelKey)
      return next
    })
  }

  const enabledDetectionModelCount = detectionModels.reduce(
    (count, model) => count + (disabledDetectionModelKeys.has(model.key) ? 0 : 1),
    0,
  )

  const changePanoramaOpacity = (opacity: number) => {
    if (controlledPanoramaOpacity === undefined) setLocalPanoramaOpacity(opacity)
    onPanoramaOpacityChange?.(opacity)
  }

  const panoramaUvAtPointer = (clientX: number, clientY: number) => {
    const runtime = runtimeRef.current
    if (!runtime) return null
    const bounds = runtime.renderer.domElement.getBoundingClientRect()
    if (!bounds.width || !bounds.height) return null
    const pointer = new THREE.Vector2(
      ((clientX - bounds.left) / bounds.width) * 2 - 1,
      -((clientY - bounds.top) / bounds.height) * 2 + 1,
    )
    const raycaster = new THREE.Raycaster()
    raycaster.setFromCamera(pointer, runtime.camera)
    const viewYaw = panoramaRayYaw(raycaster.ray.direction, yaw)
    const intersection = raycaster.intersectObject(runtime.panoramaMesh, false)[0]
    if (!intersection?.uv) return null
    return {
      u: ((intersection.uv.x % 1) + 1) % 1,
      v: 1 - intersection.uv.y,
      viewYaw,
      bounds,
    }
  }

  const manualBboxInteractive = Boolean(
    manualObject?.bboxMode &&
    (
      manualObject.proposalState.status === 'drawing' ||
      manualObject.proposalState.status === 'adjusting' ||
      manualObject.proposalState.status === 'error'
    ),
  )

  const finishManualBbox = (drag: NonNullable<typeof manualBboxDrag>) => {
    const left = Math.min(drag.startX, drag.currentX)
    const right = Math.max(drag.startX, drag.currentX)
    const top = Math.min(drag.startY, drag.currentY)
    const bottom = Math.max(drag.startY, drag.currentY)
    const first = panoramaUvAtPointer(left, top)
    if (!first || right - left < 5 || bottom - top < 5) {
      setPickFeedback('객체를 포함하도록 조금 더 큰 bbox를 그려 주세요.')
      return
    }
    const samples = [0, 0.5, 1].flatMap((xRatio) =>
      [0, 0.5, 1].map((yRatio) =>
        panoramaUvAtPointer(
          left + (right - left) * xRatio,
          top + (bottom - top) * yRatio,
        ),
      ),
    ).filter((sample): sample is NonNullable<ReturnType<typeof panoramaUvAtPointer>> => Boolean(sample))
    const geometry = seamSafeBboxFromUvSamples(
      samples,
      panoramaDimensions.width,
      panoramaDimensions.height,
    )
    if (!geometry) {
      setPickFeedback('현재 시야에서 bbox 좌표를 만들지 못했습니다. 다시 그려 주세요.')
      return
    }
    setManualBboxRect({
      left: left - first.bounds.left,
      top: top - first.bounds.top,
      width: right - left,
      height: bottom - top,
    })
    setPickFeedback(null)
    manualObject?.stageBbox(geometry)
  }

  const clearOverlayHover = () => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView
    if (hoverFrameRef.current && ownerWindow) ownerWindow.cancelAnimationFrame(hoverFrameRef.current)
    hoverFrameRef.current = 0
    pendingHoverRef.current = null
    setOverlayHover(null)
  }

  const scheduleOverlayHover = (clientX: number, clientY: number) => {
    const ownerWindow = stageRef.current?.ownerDocument.defaultView
    if (!ownerWindow) return
    pendingHoverRef.current = { x: clientX, y: clientY }
    if (hoverFrameRef.current) return
    hoverFrameRef.current = ownerWindow.requestAnimationFrame(() => {
      hoverFrameRef.current = 0
      const pending = pendingHoverRef.current
      pendingHoverRef.current = null
      if (!pending) return
      const projected = panoramaUvAtPointer(pending.x, pending.y)
      if (!projected) {
        setOverlayHover(null)
        return
      }
      const entry = panoramaOverlayAtUv(
        renderedOverlayProjection,
        projected.u,
        projected.v,
        renderedDetectionBoxes,
        {
          pointerX: pending.x - projected.bounds.left,
          pointerY: pending.y - projected.bounds.top,
          viewportWidth: projected.bounds.width,
          viewportHeight: projected.bounds.height,
          verticalFovDeg: fov,
          yawDeg: yaw,
          pitchDeg: pitch,
        },
      )
      setOverlayHover(entry ? {
        layerId: entry.layerId,
        layerName: entry.layerName,
        featureId: entry.featureId,
        properties: entry.properties ?? {},
        layerColor: entry.color,
        x: pending.x - projected.bounds.left,
        y: pending.y - projected.bounds.top,
        viewportWidth: projected.bounds.width,
        viewportHeight: projected.bounds.height,
      } : null)
    })
  }

  const selectAtPointer = async (clientX: number, clientY: number) => {
    if (!frame) return
    const projected = panoramaUvAtPointer(clientX, clientY)
    if (!projected) return
    const { u, v } = projected
    const actions = overlayActionsRef.current

    if (actions.pickMode) {
      if (!pointPayload) {
        setPickFeedback('좌표 선택용 MMS 포인트를 아직 불러오는 중입니다.')
        return
      }
      const index = nearestPanoramaPointIndex(
        u,
        v,
        pointPayload.coordinates,
        pointPayload.pointCount,
      )
      if (index === null) {
        setPickFeedback('클릭한 위치 가까이에 깊이 포인트가 없습니다. 포인트가 보이는 곳을 선택해 주세요.')
        return
      }
      const offset = index * 3
      setPickFeedback('선택한 MMS 포인트의 실제 좌표를 계산하고 있습니다.')
      try {
        const result = await api.panoramaPick(datasetId, frame.id, {
          u: pointPayload.coordinates[offset],
          v: pointPayload.coordinates[offset + 1],
          depth: pointPayload.coordinates[offset + 2],
        })
        await actions.applyPickedCoordinate(result.dataset_position, 'dataset')
        setPickFeedback(null)
      } catch (reason) {
        setPickFeedback(reason instanceof Error ? reason.message : '선택 좌표를 적용하지 못했습니다.')
      }
      return
    }

    const nearest = panoramaOverlayAtUv(
      renderedOverlayProjection,
      u,
      v,
      renderedDetectionBoxes,
      {
        pointerX: clientX - projected.bounds.left,
        pointerY: clientY - projected.bounds.top,
        viewportWidth: projected.bounds.width,
        viewportHeight: projected.bounds.height,
        verticalFovDeg: fov,
        yawDeg: yaw,
        pitchDeg: pitch,
      },
    )
    if (nearest) {
      setOverlayHover(null)
      setPinnedOverlayHover({
        layerId: nearest.layerId,
        layerName: nearest.layerName,
        featureId: nearest.featureId,
        properties: nearest.properties ?? {},
        layerColor: nearest.color,
        x: clientX - projected.bounds.left,
        y: clientY - projected.bounds.top,
        viewportWidth: projected.bounds.width,
        viewportHeight: projected.bounds.height,
      })
    } else {
      setPinnedOverlayHover(null)
      const navigation = panoramaSceneNavigationTarget(
        frame,
        onFrameChange ? frames : [],
        projected.viewYaw,
        forwardYaw,
      )
      if (navigation) {
        moveToSceneNavigation(navigation, {
          x: clientX - projected.bounds.left,
          y: clientY - projected.bounds.top,
        })
      } else if (onFrameChange) {
        showSceneNavigationFeedback('클릭한 방향에 이동할 인접 프레임이 없습니다.')
      }
    }
  }

  return (
    <div
      ref={stageRef}
      className={[
        'panorama-view',
        isDragging ? 'dragging' : '',
        manualObject?.bboxMode ? 'manual-bbox-mode' : '',
      ].filter(Boolean).join(' ')}
      tabIndex={0}
      role="region"
      aria-label="파노라마 뷰어"
      data-frame-id={frame?.id ?? ''}
      data-rendered-frame-key={renderedFrameKey ?? ''}
      data-yaw={yaw}
      data-pitch={pitch}
      data-fov={fov}
      data-media-stage={mediaStage}
      data-forward-offset={forwardOffsetDeg}
      data-point-count={pointPayload?.pointCount ?? 0}
      data-shp-point-count={renderedOverlayProjection.length}
      data-yolo-box-count={renderedDetectionBoxes.length}
      data-panorama-opacity={effectivePanoramaOpacity}
      data-pole-base-preview={String(Boolean(poleBasePreview))}
      data-manual-proposal-preview={String(Boolean(manualObjectPreview))}
      data-pole-base-marker-color={poleBaseMarkerColor}
      data-pole-base-marker-size-m={poleBaseMarkerSizeM}
      data-manual-bbox-mode={String(Boolean(manualObject?.bboxMode))}
      onPointerDown={(event) => {
        if (
          !source
          || renderedFrameKey !== `${datasetId}:${frame?.id ?? ''}`
          || loading
          || event.isPrimary === false
          || (event.pointerType !== 'touch' && event.button !== 0)
          || isPanoramaSceneControlTarget(event.target)
        ) return
        clearOverlayHover()
        event.currentTarget.focus({ preventScroll: true })
        event.currentTarget.setPointerCapture?.(event.pointerId)
        if (manualObject?.bboxMode) {
          if (!manualBboxInteractive) return
          setManualBboxDrag({
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            currentX: event.clientX,
            currentY: event.clientY,
          })
          setManualBboxRect(null)
          setIsDragging(false)
          setDragStart(null)
          return
        }
        setIsDragging(false)
        dragExceededThresholdRef.current = false
        setDragStart({ pointerId: event.pointerId, x: event.clientX, y: event.clientY, yaw, pitch })
      }}
      onPointerMove={(event) => {
        if (manualBboxDrag) {
          if (event.pointerId !== manualBboxDrag.pointerId) return
          setManualBboxDrag((current) => current ? {
            ...current,
            currentX: event.clientX,
            currentY: event.clientY,
          } : null)
          return
        }
        if (dragStart) {
          if (event.pointerId !== dragStart.pointerId) return
          if (isPanoramaSceneClick(dragStart, { x: event.clientX, y: event.clientY })) return
          dragExceededThresholdRef.current = true
          setIsDragging(true)
          setYaw(dragStart.yaw - (event.clientX - dragStart.x) * 0.12)
          setPitch(
            Math.max(-78, Math.min(78, dragStart.pitch + (event.clientY - dragStart.y) * 0.1)),
          )
        } else {
          scheduleOverlayHover(event.clientX, event.clientY)
        }
      }}
      onPointerUp={(event) => {
        if (manualBboxDrag) {
          if (event.pointerId !== manualBboxDrag.pointerId) return
          const completed = {
            ...manualBboxDrag,
            currentX: event.clientX,
            currentY: event.clientY,
          }
          setManualBboxDrag(null)
          finishManualBbox(completed)
          return
        }
        if (dragStart && event.pointerId !== dragStart.pointerId) return
        const clicked = Boolean(dragStart && !dragExceededThresholdRef.current && isPanoramaSceneClick(
          dragStart,
          { x: event.clientX, y: event.clientY },
        ))
        dragExceededThresholdRef.current = false
        setDragStart(null)
        setIsDragging(false)
        if (clicked) void selectAtPointer(event.clientX, event.clientY)
      }}
      onPointerCancel={() => {
        setManualBboxDrag(null)
        dragExceededThresholdRef.current = false
        setDragStart(null)
        setIsDragging(false)
        clearOverlayHover()
      }}
      onPointerLeave={clearOverlayHover}
    >
      {(manualBboxDrag || manualBboxRect) && (
        <div
          className="panorama-manual-bbox"
          style={manualBboxDrag ? (() => {
            const bounds = runtimeRef.current?.renderer.domElement.getBoundingClientRect()
            const left = Math.min(manualBboxDrag.startX, manualBboxDrag.currentX) - (bounds?.left ?? 0)
            const top = Math.min(manualBboxDrag.startY, manualBboxDrag.currentY) - (bounds?.top ?? 0)
            return {
              left,
              top,
              width: Math.abs(manualBboxDrag.currentX - manualBboxDrag.startX),
              height: Math.abs(manualBboxDrag.currentY - manualBboxDrag.startY),
            }
          })() : manualBboxRect ?? undefined}
          aria-hidden="true"
        >
          <i /><i /><i /><i />
        </div>
      )}
      <div
        ref={linkedPointMarkerRef}
        className="panorama-linked-point"
        aria-hidden="true"
        hidden
      />
      <OverlayHoverTooltip
        hover={pinnedOverlayHover ?? overlayHover}
        pinned={Boolean(pinnedOverlayHover)}
        onClose={() => {
          setPinnedOverlayHover(null)
          setOverlayHover(null)
        }}
        onDetails={pinnedOverlayHover?.layerId ? (hover) => {
          if (!hover.layerId) return
          overlayActionsRef.current.selectFeature({
            layerId: hover.layerId,
            featureId: hover.featureId,
          }, { navigate: false })
          openOverlayFeatureDetails(datasetId, hover)
        } : undefined}
      />
      {navigationPulse && (
        <div
          className="panorama-navigation-pulse"
          style={{ left: navigationPulse.x, top: navigationPulse.y }}
          data-target-frame-index={navigationPulse.targetFrameIndex}
          aria-hidden="true"
        >
          <i />
          <span />
        </div>
      )}
      {sceneNavigationFeedback && (
        <div className="panorama-scene-navigation-feedback" role="status">
          <Navigation size={14} aria-hidden="true" />
          <span>{sceneNavigationFeedback}</span>
        </div>
      )}
      {frame?.coordinate && (
        <div
          className="panorama-location-bar"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <span className="panorama-location-address" title={frameAddress ?? undefined}>
            <MapPin size={15} aria-hidden="true" />
            <span>
              {addressLoading
                ? '현재 주소 확인 중…'
                : frameAddress ?? `${frame.coordinate.lat.toFixed(6)}, ${frame.coordinate.lon.toFixed(6)}`}
            </span>
          </span>
        </div>
      )}
      {loading && (
        <div className="viewer-loading floating">
          <LoaderCircle className="spin" size={25} />
          <strong>파노라마 미리보기 생성 중</strong>
          <small>화면 크기에 맞춘 경량 이미지를 요청했습니다.</small>
        </div>
      )}
      {!loading && mediaStage === 'enhancing' && (
        <div className="panorama-quality-upgrade" role="status">
          <LoaderCircle className="spin" size={13} aria-hidden="true" />
          <span>고화질로 선명하게 전환 중</span>
        </div>
      )}
      {error && (
        <div className="viewer-error">
          <RefreshCcw size={25} />
          <strong>이미지를 표시할 수 없습니다</strong>
          <p>{error}</p>
          <button type="button" className="button secondary" onClick={() => setReloadKey((value) => value + 1)}>
            다시 불러오기
          </button>
        </div>
      )}
      {!frame && (
        <div className="viewer-error neutral">
          <Maximize2 size={26} />
          <strong>프레임을 선택해 주세요</strong>
          <p>왼쪽 목록에서 파노라마가 있는 프레임을 선택하세요.</p>
        </div>
      )}
      {pointPayloadRequired && (pointLoading || pointError) && (
        <div className={`panorama-point-status ${pointError ? 'error' : ''}`}>
          {pointLoading ? <LoaderCircle size={13} className="spin" /> : <ScanLine size={13} />}
          <span>
            {pointError
              ? pointError
              : pointIndexing
                ? '포인트 인덱싱 중'
                : '파노라마 포인트 갱신 중'}
          </span>
          {pointError && (
            <button
              type="button"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={() => setPointReloadKey((value) => value + 1)}
            >
              재시도
            </button>
          )}
        </div>
      )}
      {(overlayLoading || pickFeedback || detectionLoading || detectionError) && (
        <div className={`panorama-point-status shp-status ${pickFeedback || detectionError ? 'error' : ''}`}>
          {overlayLoading || detectionLoading
            ? <LoaderCircle size={13} className="spin" />
            : <Crosshair size={13} />}
          <span>
            {pickFeedback
              ?? detectionError
              ?? (overlayLoading
                ? 'SHP 포인트를 파노라마에 맞추는 중'
                : 'YOLO 검출 박스를 불러오는 중')}
          </span>
        </div>
      )}
      <div
        className="viewer-toolbar panorama-toolbar"
        onPointerDown={(event) => event.stopPropagation()}
      >
        <span>
          <MousePointer2 size={14} />
          드래그하여 둘러보기 · 장면 클릭으로 이동
        </span>
        <i />
        <label className="panorama-quality-control">
          <span>화질</span>
          <select
            aria-label="파노라마 화질"
            value={quality}
            onChange={(event) => changeQuality(event.target.value as PanoramaQuality)}
          >
            <option value="fast">빠름</option>
            <option value="high">고화질 · 4K</option>
            <option value="ultra">최고화질 · 8K</option>
          </select>
        </label>
        <label className="panorama-point-toggle" title="선택 프레임의 MMS 포인트를 파노라마에 겹쳐 표시">
          <input
            type="checkbox"
            aria-label="파노라마 포인트 오버레이 표시"
            checked={pointOverlayEnabled}
            onChange={(event) => changePointOverlay(event.target.checked)}
          />
          <ScanLine size={14} />
          포인트
        </label>
        {detectionModels.length > 0 && (
          <details className="panorama-model-filter">
            <summary title="파노라마에 표시할 YOLO 모델 선택">
              <ScanLine size={14} />
              YOLO 모델
              <b>{enabledDetectionModelCount}/{detectionModels.length}</b>
            </summary>
            <div className="panorama-model-filter-menu" role="group" aria-label="YOLO 모델 표시">
              {detectionModels.map((model) => (
                <label key={model.key}>
                  <input
                    type="checkbox"
                    checked={!disabledDetectionModelKeys.has(model.key)}
                    aria-label={`${model.name} 검출 표시`}
                    onChange={(event) => {
                      changeDetectionModelVisibility(model.key, event.target.checked)
                    }}
                  />
                  <span title={model.name}>{model.name}</span>
                  <small>{model.count.toLocaleString('ko-KR')}</small>
                </label>
              ))}
            </div>
          </details>
        )}
        <label className="panorama-opacity-control">
          <span>영상 투명도</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={panoramaOpacity}
            disabled={!hasVisualOverlay}
            aria-label="파노라마 영상 투명도"
            onChange={(event) => changePanoramaOpacity(Number(event.target.value))}
          />
        </label>
        {renderedOverlayProjection.length > 0 && (
          <span title="현재 파노라마에 투영된 SHP 포인트">
            <MapPin size={14} /> SHP {renderedOverlayProjection.length.toLocaleString('ko-KR')}
          </span>
        )}
        {poleBasePreview && (
          <span title="확정 전 지주 바닥점">
            <MapPin size={14} /> 임시 바닥점
          </span>
        )}
        {manualObjectPreview && (
          <span title="확정 전 수동 객체 위치">
            <MapPin size={14} /> 수동 제안
          </span>
        )}
        {detectionModels.length > 0 && (
          <span title="현재 파노라마의 원본 YOLO 검출 박스">
            <ScanLine size={14} /> YOLO {renderedDetectionBoxes.length.toLocaleString('ko-KR')}
          </span>
        )}
        {overlay?.pickMode && (
          <strong className="viewer-pick-indicator">
            <Crosshair size={14} /> 포인트를 클릭해 좌표 적용
          </strong>
        )}
        {manualObject?.bboxMode && (
          <strong className="viewer-pick-indicator manual-bbox-indicator">
            <Crosshair size={14} /> bbox 드래그 · Esc 취소
          </strong>
        )}
        <button type="button" onClick={() => changeZoom(6)} aria-label="축소">
          <Minus size={15} />
        </button>
        <strong>{Math.round((72 / fov) * 100)}%</strong>
        <button type="button" onClick={() => changeZoom(-6)} aria-label="확대">
          <Plus size={15} />
        </button>
        <button type="button" onClick={toggleFullscreen} aria-label="전체 화면">
          <Expand size={15} />
        </button>
      </div>
      {frame && (
        <div className="viewer-data-card">
          <span>CAM · 360°</span>
          <strong>{frame.timestamp.replace('T', ' ').slice(0, 23)}</strong>
          <small>
            {frame.coordinate
              ? `${frame.coordinate.lat.toFixed(6)}, ${frame.coordinate.lon.toFixed(6)}`
              : '좌표 없음'}
          </small>
        </div>
      )}
    </div>
  )
}
