import { List, Pencil, Pin, X } from 'lucide-react'
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { useOptionalOverlayWorkspace } from './OverlayContext'

export const OVERLAY_DETAILS_EVENT = 'mms-overlay-open-details'
export const OVERLAY_HOVER_PREVIEW_LIMIT = 4

const OVERLAY_HOVER_PREFERRED_KEYS = [
  'class_nm',
  'class_name',
  'class',
  'label',
  'name',
  'obj_type',
  'type',
  'status',
  'conf',
  'confidence',
] as const

export interface OverlayHoverState {
  identityKey?: string
  layerId?: string
  layerName: string
  featureId: string | number
  properties: Record<string, unknown>
  layerColor?: string
  x: number
  y: number
  viewportWidth: number
  viewportHeight: number
}

export interface OverlayTooltipPosition {
  left: number
  top: number
}

export function clampOverlayTooltipPosition(
  left: number,
  top: number,
  width: number,
  height: number,
  viewportWidth: number,
  viewportHeight: number,
  padding = 8,
): OverlayTooltipPosition {
  const safePadding = Math.max(0, Number.isFinite(padding) ? padding : 0)
  const maxLeft = Math.max(safePadding, viewportWidth - Math.max(0, width) - safePadding)
  const maxTop = Math.max(safePadding, viewportHeight - Math.max(0, height) - safePadding)
  return {
    left: Math.min(Math.max(left, safePadding), maxLeft),
    top: Math.min(Math.max(top, safePadding), maxTop),
  }
}

interface OverlayTooltipDragState {
  pointerId: number
  startX: number
  startY: number
  startLeft: number
  startTop: number
  width: number
  height: number
  viewportWidth: number
  viewportHeight: number
}

const OVERLAY_CLASS_NAME_KEYS = [
  'class_nm',
  'class_name',
  'class',
  'label',
  'name',
  'obj_type',
] as const

export function overlayHoverClassName(
  properties: Record<string, unknown>,
  featureId: string | number,
): string {
  const normalized = new Map(
    Object.entries(properties).map(([key, value]) => [
      key.trim().toLocaleLowerCase('en-US'),
      value,
    ]),
  )
  for (const key of OVERLAY_CLASS_NAME_KEYS) {
    const value = normalized.get(key)
    if (value === null || value === undefined) continue
    const label = String(value).trim()
    if (label) return label
  }
  return `피처 #${String(featureId)}`
}

export function overlayHoverLayerColor(
  properties: Record<string, unknown>,
  workspaceColor?: string,
): string {
  const candidate = String(workspaceColor ?? properties.__overlay_color ?? '').trim()
  return /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i.test(candidate) ? candidate : '#78909f'
}

export function overlayPropertyEntries(properties: Record<string, unknown>) {
  return Object.entries(properties).filter(([key]) => !key.startsWith('__'))
}

export function overlayPropertyPreviewEntries(
  properties: Record<string, unknown>,
  limit = OVERLAY_HOVER_PREVIEW_LIMIT,
) {
  const entries = overlayPropertyEntries(properties)
  const priority = new Map<string, number>(
    OVERLAY_HOVER_PREFERRED_KEYS.map((key, index) => [key, index]),
  )
  const meaningful = entries
    .map((entry, index) => ({ entry, index }))
    .filter(({ entry: [, value] }) => value !== null && value !== undefined && value !== '')
    .sort((left, right) => {
      const leftPriority = priority.get(left.entry[0].toLocaleLowerCase('en-US'))
      const rightPriority = priority.get(right.entry[0].toLocaleLowerCase('en-US'))
      if (leftPriority === undefined && rightPriority === undefined) return left.index - right.index
      if (leftPriority === undefined) return 1
      if (rightPriority === undefined) return -1
      return leftPriority - rightPriority || left.index - right.index
    })
    .map(({ entry }) => entry)
  const empty = entries.filter((entry) => !meaningful.includes(entry))
  return [...meaningful, ...empty].slice(0, Math.max(0, limit))
}

export function openOverlayFeatureDetails(
  datasetId: string,
  hover: Pick<OverlayHoverState, 'layerId' | 'featureId'>,
) {
  if (!datasetId || !hover.layerId) return
  window.dispatchEvent(
    new CustomEvent(OVERLAY_DETAILS_EVENT, {
      detail: {
        datasetId,
        layerId: hover.layerId,
        featureId: hover.featureId,
      },
    }),
  )
}

export function formatOverlayHoverValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'boolean') return value ? '예' : '아니요'
  if (typeof value === 'number') {
    return Number.isFinite(value)
      ? value.toLocaleString('ko-KR', { maximumFractionDigits: 6 })
      : String(value)
  }
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

export function OverlayHoverTooltip({
  hover,
  pinned = false,
  onClose,
  onDetails,
}: {
  hover: OverlayHoverState | null
  pinned?: boolean
  onClose?: () => void
  onDetails?: (hover: OverlayHoverState) => void
}) {
  const tooltipRef = useRef<HTMLElement>(null)
  const dragStateRef = useRef<OverlayTooltipDragState | null>(null)
  const overlay = useOptionalOverlayWorkspace()
  const [expanded, setExpanded] = useState(false)
  const [dragPosition, setDragPosition] = useState<OverlayTooltipPosition | null>(null)
  const [dragging, setDragging] = useState(false)
  const hoverIdentity = hover?.identityKey ?? `${hover?.layerId ?? ''}:${String(hover?.featureId ?? '')}`
  useEffect(() => {
    setExpanded(false)
    setDragPosition(null)
    setDragging(false)
    dragStateRef.current = null
  }, [hoverIdentity, pinned])
  useEffect(() => {
    if (!hover || !pinned || !onClose) return
    const ownerDocument = tooltipRef.current?.ownerDocument ?? document
    const closeOutside = (event: PointerEvent) => {
      if (!tooltipRef.current?.contains(event.target as Node)) onClose()
    }
    const closeForEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }
    // Capture makes toolbar and portal clicks reliable even when a nested
    // control stops propagation. Iframe map clicks are handled by MapView.
    ownerDocument.addEventListener('pointerdown', closeOutside, true)
    ownerDocument.addEventListener('keydown', closeForEscape)
    return () => {
      ownerDocument.removeEventListener('pointerdown', closeOutside, true)
      ownerDocument.removeEventListener('keydown', closeForEscape)
    }
  }, [hover, onClose, pinned])

  if (!hover) return null
  const alignRight = hover.x > hover.viewportWidth - 320
  const alignBottom = hover.y > hover.viewportHeight - 260
  const style: CSSProperties = dragPosition
    ? { left: dragPosition.left, top: dragPosition.top }
    : {
        ...(alignRight
          ? { right: Math.max(8, hover.viewportWidth - hover.x + 12) }
          : { left: hover.x + 12 }),
        ...(alignBottom
          ? { bottom: Math.max(8, hover.viewportHeight - hover.y + 12) }
          : { top: hover.y + 12 }),
      }
  const allEntries = overlayPropertyEntries(hover.properties)
  const previewEntries = overlayPropertyPreviewEntries(hover.properties)
  const previewKeys = new Set(previewEntries.map(([key]) => key))
  const orderedEntries = [
    ...previewEntries,
    ...allEntries.filter(([key]) => !previewKeys.has(key)),
  ]
  const entries = expanded ? orderedEntries : previewEntries
  const hiddenCount = expanded ? 0 : Math.max(0, allEntries.length - previewEntries.length)
  const className = overlayHoverClassName(hover.properties, hover.featureId)
  const workspaceColor = hover.layerId ? overlay?.layerColor(hover.layerId) : undefined
  const layerColor = overlayHoverLayerColor(
    hover.properties,
    hover.layerColor ?? workspaceColor,
  )
  const beginDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (!pinned || event.button !== 0) return
    if ((event.target as HTMLElement).closest('button')) return
    const tooltip = tooltipRef.current
    if (!tooltip) return
    const offsetParent = tooltip.offsetParent as HTMLElement | null
    const tooltipRect = tooltip.getBoundingClientRect()
    const parentRect = offsetParent?.getBoundingClientRect()
    const viewportWidth = offsetParent?.clientWidth || hover.viewportWidth
    const viewportHeight = offsetParent?.clientHeight || hover.viewportHeight
    const startPosition = clampOverlayTooltipPosition(
      tooltipRect.left - (parentRect?.left ?? 0),
      tooltipRect.top - (parentRect?.top ?? 0),
      tooltipRect.width,
      tooltipRect.height,
      viewportWidth,
      viewportHeight,
    )
    dragStateRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startLeft: startPosition.left,
      startTop: startPosition.top,
      width: tooltipRect.width,
      height: tooltipRect.height,
      viewportWidth,
      viewportHeight,
    }
    setDragPosition(startPosition)
    event.currentTarget.setPointerCapture?.(event.pointerId)
    setDragging(true)
    event.preventDefault()
    event.stopPropagation()
  }
  const continueDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragStateRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    setDragPosition(clampOverlayTooltipPosition(
      drag.startLeft + event.clientX - drag.startX,
      drag.startTop + event.clientY - drag.startY,
      drag.width,
      drag.height,
      drag.viewportWidth,
      drag.viewportHeight,
    ))
    event.preventDefault()
    event.stopPropagation()
  }
  const finishDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragStateRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    dragStateRef.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    setDragging(false)
    event.stopPropagation()
  }
  const stopPointer = (event: ReactPointerEvent) => event.stopPropagation()
  return (
    <aside
      ref={tooltipRef}
      className={`overlay-hover-tooltip ${pinned ? 'pinned' : ''} ${dragging ? 'dragging' : ''}`}
      role={pinned ? 'dialog' : 'tooltip'}
      aria-label={pinned ? `${hover.layerName} 고정 속성 미리보기` : undefined}
      data-pinned={pinned ? 'true' : 'false'}
      style={style}
      onPointerDown={stopPointer}
      onClick={(event) => event.stopPropagation()}
    >
      <header
        className={pinned ? 'overlay-hover-drag-handle' : undefined}
        title={pinned ? '드래그하여 상세정보 카드 이동' : undefined}
        onPointerDown={beginDrag}
        onPointerMove={continueDrag}
        onPointerUp={finishDrag}
        onPointerCancel={finishDrag}
      >
        <span className="overlay-hover-title">
          {pinned && <Pin size={11} aria-hidden="true" />}
          <span>
            <strong>{className}</strong>
            <small>#{String(hover.featureId)}</small>
          </span>
        </span>
        <span className="overlay-hover-layer" title={hover.layerName}>
          <i aria-hidden="true" style={{ backgroundColor: layerColor }} />
          <span>{hover.layerName}</span>
        </span>
        {pinned && onClose && (
          <button type="button" onClick={onClose} aria-label="고정 속성 닫기">
            <X size={12} />
          </button>
        )}
      </header>
      <dl>
        {entries.length ? entries.map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{formatOverlayHoverValue(value)}</dd>
          </div>
        )) : (
          <div>
            <dt>속성</dt>
            <dd>표시할 값이 없습니다.</dd>
          </div>
        )}
      </dl>
      {hiddenCount > 0 && (
        <p className="overlay-hover-more">속성 {hiddenCount.toLocaleString('ko-KR')}개 더 있음</p>
      )}
      {pinned && (
        <footer>
          <button
            type="button"
            className="secondary"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
          >
            <List size={12} /> {expanded ? '간단히' : '자세히'}
          </button>
          <button
            type="button"
            disabled={!onDetails || !hover.layerId}
            title={!onDetails || !hover.layerId ? '편집 가능한 결과 피처가 연결되지 않았습니다.' : undefined}
            onClick={() => onDetails?.(hover)}
          >
            <Pencil size={12} /> 수정하기
          </button>
        </footer>
      )}
    </aside>
  )
}
