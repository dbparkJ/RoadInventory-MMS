import { Maximize2, Pin, X } from 'lucide-react'
import {
  useEffect,
  useRef,
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
  const overlay = useOptionalOverlayWorkspace()
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
  const style: CSSProperties = {
    ...(alignRight ? { right: Math.max(8, hover.viewportWidth - hover.x + 12) } : { left: hover.x + 12 }),
    ...(alignBottom ? { bottom: Math.max(8, hover.viewportHeight - hover.y + 12) } : { top: hover.y + 12 }),
  }
  const allEntries = overlayPropertyEntries(hover.properties)
  const entries = overlayPropertyPreviewEntries(hover.properties)
  const hiddenCount = Math.max(0, allEntries.length - entries.length)
  const className = overlayHoverClassName(hover.properties, hover.featureId)
  const workspaceColor = hover.layerId ? overlay?.layerColor(hover.layerId) : undefined
  const layerColor = overlayHoverLayerColor(
    hover.properties,
    hover.layerColor ?? workspaceColor,
  )
  const stopPointer = (event: ReactPointerEvent) => event.stopPropagation()
  return (
    <aside
      ref={tooltipRef}
      className={`overlay-hover-tooltip ${pinned ? 'pinned' : ''}`}
      role={pinned ? 'dialog' : 'tooltip'}
      aria-label={pinned ? `${hover.layerName} 고정 속성 미리보기` : undefined}
      data-pinned={pinned ? 'true' : 'false'}
      style={style}
      onPointerDown={stopPointer}
      onClick={(event) => event.stopPropagation()}
    >
      <header>
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
      {pinned && onDetails && hover.layerId && (
        <footer>
          <button type="button" onClick={() => onDetails(hover)}>
            <Maximize2 size={12} /> 자세히
          </button>
        </footer>
      )}
    </aside>
  )
}
