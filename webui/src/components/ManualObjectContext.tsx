import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError } from '../lib/api'
import { hasOpenModalDialog, isWorkspaceShortcutBlockedTarget } from '../lib/frameNavigation'
import type {
  EquirectangularBBoxGeometry,
  Frame,
  GeometryProposal,
  ManualDuplicatePreflightResponse,
  ManualObjectTemplate,
  ManualObjectTemplateId,
  ManualObservation,
  OverlayField,
  OverlayLayer,
  ReviewTask,
} from '../types'
import {
  useOverlayWorkspace,
  type PoleBaseTemplateOptions,
} from './OverlayContext'
import { isReviewTaskComplete, useOptionalReviewWorkspace } from './ReviewContext'
import './ManualObjectWorkspace.css'

const FALLBACK_TEMPLATES: ManualObjectTemplate[] = [
  {
    template_id: 'TRAFFIC_SIGN',
    class_name: 'TRAFFIC_SIGN',
    geometry_type: 'Point',
    tool_id: 'panorama_bbox_point_v1',
    duplicate_radius_m: 0.75,
    continuous: true,
    required_semantics: ['class'],
    relation_semantics: ['support_id'],
  },
  {
    template_id: 'SIGN_SUPPORT_POLE',
    class_name: 'SIGN_SUPPORT_POLE',
    geometry_type: 'Point',
    tool_id: 'manual_pole_base_v1',
    duplicate_radius_m: 0.5,
    continuous: true,
    required_semantics: ['class'],
    relation_semantics: [],
  },
]

const CLASS_FIELD_ALIASES = new Set([
  'CLASS',
  'CLASS_NM',
  'CLASSNAME',
  'CLASS_NAME',
  'OBJ_TYPE',
  'TYPE',
])

export const MANUAL_PROPOSAL_PREPARING_MAX_ATTEMPTS = 8
export const MANUAL_PROPOSAL_PREPARING_RETRY_MS = 2_000

interface StoredManualObjectPreferences {
  templateId?: ManualObjectTemplateId
  targetLayers?: Partial<Record<ManualObjectTemplateId, string>>
  properties?: Partial<Record<ManualObjectTemplateId, Record<string, unknown>>>
  continuous?: Partial<Record<ManualObjectTemplateId, boolean>>
}

export interface ManualProposalReadyData {
  frameId: string
  targetLayerId: string
  templateId: 'TRAFFIC_SIGN'
  geometry: EquirectangularBBoxGeometry
  observation: ManualObservation
  proposal: GeometryProposal
  duplicate: ManualDuplicatePreflightResponse
  idempotencyKey: string
  reviewTaskSnapshot: ManualReviewTaskSnapshot | null
  saveError?: string
}

export interface ManualReviewTaskSnapshot {
  id: string
  sessionId: string
  datasetId: string
  targetLayerId: string | null
  frameId: string | null
  trackId: string | null
  frameStart: number | null
  frameEnd: number | null
}

export type ManualProposalState =
  | { status: 'idle' }
  | { status: 'drawing'; frameId: string }
  | { status: 'adjusting'; frameId: string; geometry: EquirectangularBBoxGeometry }
  | {
      status: 'loading'
      frameId: string
      geometry: EquirectangularBBoxGeometry
      observation?: ManualObservation
      preparingAttempt?: number
      preparingMaxAttempts?: number
    }
  | { status: 'ready'; data: ManualProposalReadyData }
  | { status: 'committing'; data: ManualProposalReadyData }
  | {
      status: 'error'
      frameId: string
      geometry?: EquirectangularBBoxGeometry
      observation?: ManualObservation
      message: string
      reasonCodes: string[]
    }

export interface ManualObjectContextValue {
  enabled: boolean
  frame: Frame | null
  templates: ManualObjectTemplate[]
  templatesLoading: boolean
  templateId: ManualObjectTemplateId
  template: ManualObjectTemplate
  setTemplateId: (templateId: ManualObjectTemplateId) => void
  pointLayers: OverlayLayer[]
  targetLayerId: string
  targetLayer: OverlayLayer | null
  setTargetLayerId: (layerId: string) => void
  properties: Record<string, unknown>
  effectiveProperties: Record<string, unknown>
  setProperty: (name: string, value: unknown) => void
  missingRequiredFields: string[]
  continuous: boolean
  setContinuous: (continuous: boolean) => void
  bboxMode: boolean
  proposalState: ManualProposalState
  proposalPosition: [number, number, number] | null
  allowNearDuplicate: boolean
  setAllowNearDuplicate: (allow: boolean) => void
  duplicateOverrideReason: string
  setDuplicateOverrideReason: (reason: string) => void
  startSelectedTemplate: () => void
  beginTrafficSignBbox: () => void
  stageBbox: (geometry: EquirectangularBBoxGeometry) => void
  submitStagedBbox: () => Promise<void>
  submitBbox: (geometry: EquirectangularBBoxGeometry) => Promise<void>
  retryProposal: () => Promise<void>
  retryBbox: () => void
  cancel: () => void
  confirmProposal: (nextTask?: boolean) => Promise<void>
  canConfirmAndNext: boolean
  reviewTaskLinkChanged: boolean
}

const ManualObjectContext = createContext<ManualObjectContextValue | null>(null)

function normalizedFieldName(name: string): string {
  return name.trim().toUpperCase().replace(/\s+/g, '_')
}

function hasPropertyValue(value: unknown): boolean {
  return value !== null && value !== undefined && (typeof value !== 'string' || value.trim() !== '')
}

export function missingManualRequiredFields(
  fields: readonly OverlayField[],
  properties: Record<string, unknown>,
): string[] {
  return fields
    .filter(
      (field) =>
        field.required &&
        !field.internal &&
        !CLASS_FIELD_ALIASES.has(normalizedFieldName(field.name)) &&
        !hasPropertyValue(properties[field.name]),
    )
    .map((field) => field.name)
}

export function manualEffectiveProperties(
  template: ManualObjectTemplate,
  fields: readonly OverlayField[],
  properties: Record<string, unknown>,
): Record<string, unknown> {
  const classValues = Object.fromEntries(
    fields
      .filter((field) => CLASS_FIELD_ALIASES.has(normalizedFieldName(field.name)))
      .map((field) => [field.name, template.class_name]),
  )
  return {
    ...(template.default_values ?? template.default_properties ?? {}),
    ...properties,
    ...(template.fixed_values ?? {}),
    ...classValues,
  }
}

export function seamSafeBboxFromUv(
  start: { u: number; v: number },
  end: { u: number; v: number },
  imageWidth: number,
  imageHeight: number,
): EquirectangularBBoxGeometry | null {
  return seamSafeBboxFromUvSamples([start, end], imageWidth, imageHeight)
}

export function seamSafeBboxFromUvSamples(
  samples: ReadonlyArray<{ u: number; v: number }>,
  imageWidth: number,
  imageHeight: number,
): EquirectangularBBoxGeometry | null {
  if (
    samples.length < 2 ||
    !samples.every((sample) => Number.isFinite(sample.u) && Number.isFinite(sample.v)) ||
    ![imageWidth, imageHeight].every(Number.isFinite) ||
    imageWidth <= 0 ||
    imageHeight <= 0
  ) {
    return null
  }
  const uValues = samples.map((sample) => ((sample.u % 1) + 1) % 1).sort((left, right) => left - right)
  const vValues = samples.map((sample) => Math.min(1, Math.max(0, sample.v)))
  const vMin = Math.min(...vValues)
  const vMax = Math.max(...vValues)
  if (vMax - vMin < 1 / imageHeight) return null

  let largestGap = -1
  let largestGapIndex = 0
  for (let index = 0; index < uValues.length; index += 1) {
    const current = uValues[index]
    const next = index === uValues.length - 1 ? uValues[0] + 1 : uValues[index + 1]
    const gap = next - current
    if (gap > largestGap) {
      largestGap = gap
      largestGapIndex = index
    }
  }
  const arcStart = uValues[(largestGapIndex + 1) % uValues.length]
  const arcEnd = uValues[largestGapIndex]
  let intervals: Array<[number, number]>
  if (arcStart <= arcEnd) {
    intervals = [[arcStart, arcEnd]]
  } else {
    intervals = []
    if (arcStart < 1) intervals.push([arcStart, 1])
    if (arcEnd > 0) intervals.push([0, arcEnd])
  }
  const totalWidth = intervals.reduce((sum, [left, right]) => sum + right - left, 0)
  if (totalWidth < 1 / imageWidth || intervals.length === 0) return null
  return {
    type: 'equirectangular_bbox',
    u_intervals: intervals,
    v_min: vMin,
    v_max: vMax,
    image_width: Math.max(1, Math.round(imageWidth)),
    image_height: Math.max(1, Math.round(imageHeight)),
  }
}

function preferenceKey(datasetId: string): string {
  return `mms.manual-object:${datasetId}`
}

function readPreferences(datasetId: string): StoredManualObjectPreferences {
  if (!datasetId) return {}
  try {
    const value = window.localStorage.getItem(preferenceKey(datasetId))
    return value ? (JSON.parse(value) as StoredManualObjectPreferences) : {}
  } catch {
    return {}
  }
}

function writePreferences(datasetId: string, value: StoredManualObjectPreferences): void {
  if (!datasetId) return
  try {
    window.localStorage.setItem(preferenceKey(datasetId), JSON.stringify(value))
  } catch {
    // Preferences are optional; the server proposal remains authoritative.
  }
}

function requestKey(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}_${Math.random().toString(16).slice(2)}`
  return `${prefix}_${random}`
}

function waitForManualProposalRetry(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason ?? new DOMException('Aborted', 'AbortError'))
      return
    }
    const timeoutId = window.setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, MANUAL_PROPOSAL_PREPARING_RETRY_MS)
    const abort = () => {
      window.clearTimeout(timeoutId)
      reject(signal.reason ?? new DOMException('Aborted', 'AbortError'))
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}

function proposalFromState(state: ManualProposalState): GeometryProposal | null {
  return state.status === 'ready' || state.status === 'committing' ? state.data.proposal : null
}

function reviewTaskCoversFrame(task: ReviewTask, frame: Frame): boolean {
  const hasRange = task.frame_start != null && task.frame_end != null
  if (hasRange) {
    if (frame.index < task.frame_start! || frame.index > task.frame_end!) return false
  } else if (task.frame_id !== frame.id) {
    return false
  }
  return task.track_id === null || task.track_id === frame.track_id
}

function linkableReviewTask(
  task: ReviewTask | null | undefined,
  sessionId: string | null | undefined,
  datasetId: string,
  layerId: string,
  frame: Frame,
): ReviewTask | null {
  if (
    !task ||
    sessionId !== task.session_id ||
    task.dataset_id !== datasetId ||
    task.status !== 'in_progress' ||
    (task.target_layer_id !== null && task.target_layer_id !== layerId) ||
    !reviewTaskCoversFrame(task, frame)
  ) {
    return null
  }
  return task
}

function snapshotReviewTask(task: ReviewTask | null): ManualReviewTaskSnapshot | null {
  if (!task) return null
  return {
    id: task.id,
    sessionId: task.session_id,
    datasetId: task.dataset_id,
    targetLayerId: task.target_layer_id,
    frameId: task.frame_id,
    trackId: task.track_id,
    frameStart: task.frame_start,
    frameEnd: task.frame_end,
  }
}

function reviewTaskHasSnapshotScope(
  task: ReviewTask | null | undefined,
  sessionId: string | null | undefined,
  snapshot: ManualReviewTaskSnapshot,
): task is ReviewTask {
  return Boolean(
    task &&
    sessionId === snapshot.sessionId &&
    task.id === snapshot.id &&
    task.session_id === snapshot.sessionId &&
    task.dataset_id === snapshot.datasetId &&
    task.target_layer_id === snapshot.targetLayerId &&
    task.frame_id === snapshot.frameId &&
    task.track_id === snapshot.trackId &&
    task.frame_start === snapshot.frameStart &&
    task.frame_end === snapshot.frameEnd
  )
}

function reviewTaskMatchesSnapshot(
  task: ReviewTask | null | undefined,
  sessionId: string | null | undefined,
  snapshot: ManualReviewTaskSnapshot,
): task is ReviewTask {
  return task?.status === 'in_progress' && reviewTaskHasSnapshotScope(task, sessionId, snapshot)
}

export function ManualObjectProvider({
  enabled,
  datasetId,
  frame,
  notify,
  children,
}: {
  enabled: boolean
  datasetId: string
  frame: Frame | null
  notify?: (entry: { tone: 'success' | 'error' | 'info'; title: string; message?: string }) => void
  children: ReactNode
}) {
  const overlay = useOverlayWorkspace()
  const review = useOptionalReviewWorkspace()
  const [templates, setTemplates] = useState<ManualObjectTemplate[]>(FALLBACK_TEMPLATES)
  const [templatesLoading, setTemplatesLoading] = useState(false)
  const [templateId, setTemplateIdState] = useState<ManualObjectTemplateId>('TRAFFIC_SIGN')
  const [targetLayerId, setTargetLayerIdState] = useState('')
  const [preferencesDatasetId, setPreferencesDatasetId] = useState('')
  const [properties, setProperties] = useState<Record<string, unknown>>({})
  const [continuous, setContinuousState] = useState(true)
  const [bboxMode, setBboxMode] = useState(false)
  const [proposalState, setProposalState] = useState<ManualProposalState>({ status: 'idle' })
  const [allowNearDuplicate, setAllowNearDuplicate] = useState(false)
  const [duplicateOverrideReason, setDuplicateOverrideReason] = useState('')
  const requestRef = useRef<{ id: number; controller: AbortController } | null>(null)
  const requestIdRef = useRef(0)
  const commitInFlightRef = useRef(false)
  const mountedRef = useRef(true)
  const scopeRef = useRef(`${datasetId}:${frame?.id ?? ''}`)
  const preferencesRef = useRef<StoredManualObjectPreferences>({})
  const bboxReviewTaskSnapshotRef = useRef<ManualReviewTaskSnapshot | null>(null)
  const reviewRef = useRef(review)
  reviewRef.current = review
  const stateRef = useRef(proposalState)
  stateRef.current = proposalState

  const pointLayers = useMemo(
    () => overlay.layers.filter((layer) => layer.geometry_type.toLowerCase() === 'point'),
    [overlay.layers],
  )
  const template =
    templates.find((candidate) => candidate.template_id === templateId) ?? FALLBACK_TEMPLATES[0]
  const targetLayer = pointLayers.find((layer) => layer.id === targetLayerId) ?? null
  const targetFields =
    targetLayer?.fields ??
    (targetLayer ? overlay.features[targetLayer.id]?.dataset?.fields : undefined) ??
    []
  const effectiveProperties = useMemo(
    () => manualEffectiveProperties(template, targetFields, properties),
    [properties, targetFields, template],
  )
  const templateMissingRequiredFields = useMemo(
    () => missingManualRequiredFields(targetFields, effectiveProperties),
    [effectiveProperties, targetFields],
  )
  const poleTemplateOptions = useMemo<PoleBaseTemplateOptions>(() => {
    const fieldNames = new Set(targetFields.map((field) => field.name))
    return {
      templateId: 'SIGN_SUPPORT_POLE',
      properties: Object.fromEntries(
        Object.entries(effectiveProperties).filter(([name]) => fieldNames.has(name)),
      ),
      requiredFields: targetFields
        .filter((field) => field.required && !field.internal)
        .map((field) => field.name),
      allowNearDuplicate,
      overrideReason: duplicateOverrideReason,
    }
  }, [allowNearDuplicate, duplicateOverrideReason, effectiveProperties, targetFields])
  const poleTemplateValidation =
    templateId === 'SIGN_SUPPORT_POLE' && overlay.poleBaseProposal.status === 'ready'
      ? overlay.poleBaseProposal.templateValidation
      : undefined
  const missingRequiredFields =
    poleTemplateValidation?.missingRequiredFields ?? templateMissingRequiredFields

  useEffect(() => {
    if (templateId !== 'SIGN_SUPPORT_POLE' || !targetLayerId) return
    overlay.updateStagedPoleBaseTemplateOptions(targetLayerId, poleTemplateOptions)
  }, [overlay.updateStagedPoleBaseTemplateOptions, poleTemplateOptions, targetLayerId, templateId])

  const warnIfReviewTaskWillNotLink = useCallback((layerId = targetLayerId) => {
    const task = review?.currentTask
    if (!task || (frame && linkableReviewTask(task, review?.session?.id, datasetId, layerId, frame))) return
    notify?.({
      tone: 'info',
      title: '현재 검수 항목에는 자동 연결하지 않습니다',
      message: isReviewTaskComplete(task)
        ? '이미 종료된 검수 항목입니다. 객체는 저장할 수 있지만 해당 task는 변경하지 않습니다.'
        : task.status === 'todo'
          ? '검수 항목 claim이 완료되지 않았습니다. 목록을 새로고침하거나 항목을 다시 선택해 주세요.'
        : task.target_layer_id && task.target_layer_id !== layerId
          ? '검수 항목과 선택한 대상 레이어가 다릅니다. 대상 레이어를 확인해 주세요.'
          : '현재 검수 범위와 수동 객체 작업 범위가 다릅니다.',
    })
  }, [datasetId, frame, notify, review?.currentTask, review?.session?.id, targetLayerId])

  const abortRequest = useCallback(() => {
    requestIdRef.current += 1
    requestRef.current?.controller.abort()
    requestRef.current = null
  }, [])

  const forgetServerProposal = useCallback((state: ManualProposalState) => {
    const proposal = proposalFromState(state)
    if (proposal) void api.deleteManualObjectProposal(proposal.proposal_id).catch(() => undefined)
  }, [])

  const persistPreferences = useCallback(
    (patch: Partial<StoredManualObjectPreferences>) => {
      const next = { ...preferencesRef.current, ...patch }
      preferencesRef.current = next
      writePreferences(datasetId, next)
    },
    [datasetId],
  )

  useEffect(() => {
    const stored = readPreferences(datasetId)
    preferencesRef.current = stored
    const nextTemplate = stored.templateId ?? 'TRAFFIC_SIGN'
    setTemplateIdState(nextTemplate)
    setProperties(stored.properties?.[nextTemplate] ?? {})
    setContinuousState(stored.continuous?.[nextTemplate] ?? true)
    setTargetLayerIdState(stored.targetLayers?.[nextTemplate] ?? '')
    setPreferencesDatasetId(datasetId)
  }, [datasetId])

  useEffect(() => {
    if (!enabled || !datasetId) {
      setTemplates(FALLBACK_TEMPLATES)
      setTemplatesLoading(false)
      return
    }
    const controller = new AbortController()
    setTemplatesLoading(true)
    void api.manualObjectTemplates(controller.signal)
      .then((response) => {
        if (!controller.signal.aborted && response.items.length > 0) setTemplates(response.items)
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return
        notify?.({
          tone: 'error',
          title: '객체 템플릿을 불러오지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
      })
      .finally(() => {
        if (!controller.signal.aborted) setTemplatesLoading(false)
      })
    return () => controller.abort()
  }, [datasetId, enabled, notify])

  useEffect(() => {
    if (preferencesDatasetId !== datasetId) return
    if (!enabled || pointLayers.length === 0) {
      setTargetLayerIdState('')
      return
    }
    if (pointLayers.some((layer) => layer.id === targetLayerId)) return
    const remembered = preferencesRef.current.targetLayers?.[templateId]
    const sessionLayer = review?.session?.target_layer_ids.find((layerId) =>
      pointLayers.some((layer) => layer.id === layerId),
    )
    const next =
      (remembered && pointLayers.some((layer) => layer.id === remembered) ? remembered : '') ||
      sessionLayer ||
      (pointLayers.some((layer) => layer.id === overlay.activeLayerId) ? overlay.activeLayerId : '') ||
      pointLayers[0].id
    setTargetLayerIdState(next)
  }, [datasetId, enabled, overlay.activeLayerId, pointLayers, preferencesDatasetId, review?.session?.target_layer_ids, targetLayerId, templateId])

  useEffect(() => {
    const scope = `${datasetId}:${frame?.id ?? ''}`
    if (scopeRef.current === scope) return
    scopeRef.current = scope
    const previous = stateRef.current
    if (!commitInFlightRef.current) abortRequest()
    if (previous.status !== 'committing') forgetServerProposal(previous)
    bboxReviewTaskSnapshotRef.current =
      bboxMode && continuous && frame
        ? snapshotReviewTask(linkableReviewTask(review?.currentTask, review?.session?.id, datasetId, targetLayerId, frame))
        : null
    setProposalState(
      bboxMode && continuous && frame ? { status: 'drawing', frameId: frame.id } : { status: 'idle' },
    )
    if (!continuous) setBboxMode(false)
  }, [abortRequest, bboxMode, continuous, datasetId, forgetServerProposal, frame, review?.currentTask, review?.session?.id, targetLayerId])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (!commitInFlightRef.current) requestRef.current?.controller.abort()
      const current = stateRef.current
      if (current.status !== 'committing') forgetServerProposal(current)
    }
  }, [forgetServerProposal])

  const setTemplateId = useCallback(
    (nextTemplateId: ManualObjectTemplateId) => {
      if (nextTemplateId === templateId) return
      if (stateRef.current.status === 'committing' || commitInFlightRef.current) {
        notify?.({ tone: 'info', title: '저장이 끝난 뒤 객체 종류를 변경해 주세요.' })
        return
      }
      abortRequest()
      forgetServerProposal(stateRef.current)
      bboxReviewTaskSnapshotRef.current = null
      setBboxMode(false)
      setProposalState({ status: 'idle' })
      if (overlay.poleBaseProposal.status !== 'idle') overlay.cancelPoleBaseProposal()
      setTemplateIdState(nextTemplateId)
      const stored = preferencesRef.current
      setProperties(stored.properties?.[nextTemplateId] ?? {})
      setContinuousState(stored.continuous?.[nextTemplateId] ?? true)
      setTargetLayerIdState(stored.targetLayers?.[nextTemplateId] ?? '')
      persistPreferences({ templateId: nextTemplateId })
    },
    [abortRequest, forgetServerProposal, notify, overlay, persistPreferences, templateId],
  )

  const setTargetLayerId = useCallback(
    (layerId: string) => {
      if (layerId === targetLayerId) return
      if (stateRef.current.status === 'committing' || commitInFlightRef.current) {
        notify?.({ tone: 'info', title: '저장이 끝난 뒤 대상 레이어를 변경해 주세요.' })
        return
      }
      abortRequest()
      forgetServerProposal(stateRef.current)
      bboxReviewTaskSnapshotRef.current = null
      setBboxMode(false)
      setProposalState({ status: 'idle' })
      if (overlay.poleBaseProposal.status !== 'idle') overlay.cancelPoleBaseProposal()
      setTargetLayerIdState(layerId)
      persistPreferences({
        targetLayers: { ...preferencesRef.current.targetLayers, [templateId]: layerId },
      })
      if (layerId) overlay.setActiveLayerId(layerId)
    },
    [abortRequest, forgetServerProposal, notify, overlay, persistPreferences, targetLayerId, templateId],
  )

  const setProperty = useCallback(
    (name: string, value: unknown) => {
      setProperties((current) => {
        const next = { ...current, [name]: value }
        persistPreferences({
          properties: { ...preferencesRef.current.properties, [templateId]: next },
        })
        return next
      })
    },
    [persistPreferences, templateId],
  )

  const setContinuous = useCallback(
    (value: boolean) => {
      setContinuousState(value)
      persistPreferences({
        continuous: { ...preferencesRef.current.continuous, [templateId]: value },
      })
    },
    [persistPreferences, templateId],
  )

  const beginTrafficSignBbox = useCallback(() => {
    if (stateRef.current.status === 'committing' || commitInFlightRef.current) {
      notify?.({ tone: 'info', title: '수동 객체를 저장하고 있습니다.' })
      return
    }
    if (!enabled || !frame) {
      notify?.({ tone: 'info', title: '수동 bbox를 그릴 파노라마 프레임을 먼저 선택해 주세요.' })
      return
    }
    const rememberedTrafficLayer = preferencesRef.current.targetLayers?.TRAFFIC_SIGN
    const nextTargetLayerId = templateId === 'TRAFFIC_SIGN'
      ? targetLayerId
      : (rememberedTrafficLayer && pointLayers.some((candidate) => candidate.id === rememberedTrafficLayer)
          ? rememberedTrafficLayer
          : review?.session?.target_layer_ids.find((layerId) =>
              pointLayers.some((candidate) => candidate.id === layerId),
            ) ?? pointLayers[0]?.id ?? '')
    const layer = pointLayers.find((candidate) => candidate.id === nextTargetLayerId)
    if (!layer) {
      notify?.({ tone: 'info', title: 'Point 대상 레이어를 먼저 선택해 주세요.' })
      return
    }
    if (templateId !== 'TRAFFIC_SIGN') {
      setTemplateIdState('TRAFFIC_SIGN')
      setProperties(preferencesRef.current.properties?.TRAFFIC_SIGN ?? {})
      setContinuousState(preferencesRef.current.continuous?.TRAFFIC_SIGN ?? true)
      setTargetLayerIdState(layer.id)
      persistPreferences({ templateId: 'TRAFFIC_SIGN' })
    }
    warnIfReviewTaskWillNotLink(layer.id)
    abortRequest()
    forgetServerProposal(stateRef.current)
    bboxReviewTaskSnapshotRef.current = snapshotReviewTask(
      linkableReviewTask(review?.currentTask, review?.session?.id, datasetId, layer.id, frame),
    )
    if (overlay.poleBaseProposal.status !== 'idle') overlay.cancelPoleBaseProposal()
    setAllowNearDuplicate(false)
    setDuplicateOverrideReason('')
    setBboxMode(true)
    setProposalState({ status: 'drawing', frameId: frame.id })
    overlay.setActiveLayerId(layer.id)
  }, [abortRequest, datasetId, enabled, forgetServerProposal, frame, notify, overlay, persistPreferences, pointLayers, review?.currentTask, review?.session?.id, review?.session?.target_layer_ids, targetLayerId, templateId, warnIfReviewTaskWillNotLink])

  const submitBbox = useCallback(
    async (geometry: EquirectangularBBoxGeometry, existingObservation?: ManualObservation) => {
      if (!enabled || !datasetId || !frame || !targetLayer || templateId !== 'TRAFFIC_SIGN') return
      abortRequest()
      forgetServerProposal(stateRef.current)
      const requestId = requestIdRef.current + 1
      requestIdRef.current = requestId
      const controller = new AbortController()
      requestRef.current = { id: requestId, controller }
      const expectedScope = `${datasetId}:${frame.id}`
      setProposalState({
        status: 'loading',
        frameId: frame.id,
        geometry,
        ...(existingObservation ? { observation: existingObservation } : {}),
      })
      setAllowNearDuplicate(false)
      setDuplicateOverrideReason('')
      let serverProposalId = ''
      let observation = existingObservation
      try {
        if (!observation) {
          const observationResponse = await api.createManualObservation(
            datasetId,
            frame.id,
            {
              target_layer_id: targetLayer.id,
              template_id: 'TRAFFIC_SIGN',
              geometry_2d: geometry,
              created_by: 'operator-local',
            },
            controller.signal,
          )
          observation = observationResponse.observation
        }
        let proposalResponse: Awaited<ReturnType<typeof api.createManualObjectProposal>> | null = null
        for (let attempt = 1; attempt <= MANUAL_PROPOSAL_PREPARING_MAX_ATTEMPTS; attempt += 1) {
          try {
            proposalResponse = await api.createManualObjectProposal(
              datasetId,
              frame.id,
              {
                target_layer_id: targetLayer.id,
                observation_id: observation.observation_id,
                template_id: 'TRAFFIC_SIGN',
                property_patch: effectiveProperties,
              },
              controller.signal,
            )
            break
          } catch (reason) {
            const catalogPreparing =
              reason instanceof ApiError &&
              (reason.status === 202 || reason.code === 'CATALOG_PREPARING')
            if (!catalogPreparing || attempt >= MANUAL_PROPOSAL_PREPARING_MAX_ATTEMPTS) {
              if (catalogPreparing) {
                throw new ApiError(
                  `원본 점군 준비가 지연되어 자동 재시도 ${MANUAL_PROPOSAL_PREPARING_MAX_ATTEMPTS}회를 마쳤습니다. 계산 재시도를 눌러 계속할 수 있습니다.`,
                  202,
                  'CATALOG_PREPARING',
                  reason.details,
                )
              }
              throw reason
            }
            if (
              controller.signal.aborted ||
              requestIdRef.current !== requestId ||
              scopeRef.current !== expectedScope
            ) return
            setProposalState({
              status: 'loading',
              frameId: frame.id,
              geometry,
              observation,
              preparingAttempt: attempt + 1,
              preparingMaxAttempts: MANUAL_PROPOSAL_PREPARING_MAX_ATTEMPTS,
            })
            await waitForManualProposalRetry(controller.signal)
          }
        }
        if (!proposalResponse) return
        const proposal = proposalResponse.proposal
        serverProposalId = proposal.proposal_id
        if (
          controller.signal.aborted ||
          requestIdRef.current !== requestId ||
          scopeRef.current !== expectedScope
        ) {
          void api.deleteManualObjectProposal(proposal.proposal_id).catch(() => undefined)
          return
        }
        if (proposal.status === 'failed' || !proposal.geometry) {
          void api.deleteManualObjectProposal(proposal.proposal_id).catch(() => undefined)
          setProposalState({
            status: 'error',
            frameId: frame.id,
            geometry,
            observation,
            message: proposal.reason_codes.join(' · ') || 'bbox에서 3D 위치를 산출하지 못했습니다.',
            reasonCodes: proposal.reason_codes,
          })
          return
        }
        const duplicate = await api.duplicateManualObjectPreflight(
          datasetId,
          {
            target_layer_id: targetLayer.id,
            template_id: 'TRAFFIC_SIGN',
            position: proposal.geometry.coordinates,
            observation_id: observation.observation_id,
          },
          controller.signal,
        )
        if (
          controller.signal.aborted ||
          requestIdRef.current !== requestId ||
          scopeRef.current !== expectedScope
        ) {
          void api.deleteManualObjectProposal(proposal.proposal_id).catch(() => undefined)
          return
        }
        setProposalState({
          status: 'ready',
          data: {
            frameId: frame.id,
            targetLayerId: targetLayer.id,
            templateId: 'TRAFFIC_SIGN',
            geometry,
            observation,
            proposal,
            duplicate,
            idempotencyKey: requestKey('manual_commit'),
            reviewTaskSnapshot: bboxReviewTaskSnapshotRef.current,
          },
        })
      } catch (reason) {
        if (serverProposalId) {
          void api.deleteManualObjectProposal(serverProposalId).catch(() => undefined)
        }
        if (controller.signal.aborted || requestIdRef.current !== requestId) return
        const message = reason instanceof Error ? reason.message : '수동 객체 제안을 만들지 못했습니다.'
        setProposalState({
          status: 'error',
          frameId: frame.id,
          geometry,
          ...(observation ? { observation } : {}),
          message,
          reasonCodes: reason instanceof ApiError && reason.code ? [reason.code] : [],
        })
        notify?.({ tone: reason instanceof ApiError && reason.status === 202 ? 'info' : 'error', title: '수동 객체 제안을 만들지 못했습니다', message })
      } finally {
        if (requestRef.current?.id === requestId) requestRef.current = null
      }
    },
    [abortRequest, datasetId, effectiveProperties, enabled, forgetServerProposal, frame, notify, targetLayer, templateId],
  )

  const stageBbox = useCallback(
    (geometry: EquirectangularBBoxGeometry) => {
      const current = stateRef.current
      if (
        !bboxMode ||
        !frame ||
        requestRef.current ||
        commitInFlightRef.current
      ) return
      if (
        current.status !== 'drawing' &&
        current.status !== 'adjusting' &&
        current.status !== 'error'
      ) return
      if (current.frameId !== frame.id) return
      // A released bbox is already an explicit operator action. Start the 3D
      // proposal immediately so detached panorama users never have to find a
      // second submit button in the main workspace.
      void submitBbox(geometry)
    },
    [bboxMode, frame, submitBbox],
  )

  const submitStagedBbox = useCallback(async () => {
    const current = stateRef.current
    if (current.status !== 'adjusting') return
    await submitBbox(current.geometry)
  }, [submitBbox])

  const retryProposal = useCallback(async () => {
    const current = stateRef.current
    if (
      current.status !== 'error' ||
      !current.geometry ||
      !frame ||
      current.frameId !== frame.id ||
      (current.observation && (
        current.observation.dataset_id !== datasetId || current.observation.frame_id !== frame.id
      ))
    ) return
    await submitBbox(current.geometry, current.observation)
  }, [datasetId, frame, submitBbox])

  const retryBbox = useCallback(() => {
    if (stateRef.current.status === 'committing' || commitInFlightRef.current) {
      notify?.({ tone: 'info', title: '저장이 끝난 뒤 bbox를 다시 선택해 주세요.' })
      return
    }
    abortRequest()
    forgetServerProposal(stateRef.current)
    bboxReviewTaskSnapshotRef.current = frame
      ? snapshotReviewTask(linkableReviewTask(review?.currentTask, review?.session?.id, datasetId, targetLayerId, frame))
      : null
    setAllowNearDuplicate(false)
    setDuplicateOverrideReason('')
    if (frame) {
      setBboxMode(true)
      setProposalState({ status: 'drawing', frameId: frame.id })
    } else {
      setBboxMode(false)
      setProposalState({ status: 'idle' })
    }
  }, [abortRequest, datasetId, forgetServerProposal, frame, notify, review?.currentTask, review?.session?.id, targetLayerId])

  const cancel = useCallback(() => {
    if (stateRef.current.status === 'committing' || commitInFlightRef.current) {
      notify?.({ tone: 'info', title: '수동 객체를 저장하고 있습니다.' })
      return
    }
    abortRequest()
    forgetServerProposal(stateRef.current)
    bboxReviewTaskSnapshotRef.current = null
    setBboxMode(false)
    setProposalState({ status: 'idle' })
    setAllowNearDuplicate(false)
    setDuplicateOverrideReason('')
    if (overlay.poleBaseProposal.status !== 'idle') overlay.cancelPoleBaseProposal()
  }, [abortRequest, forgetServerProposal, notify, overlay])

  const startSelectedTemplate = useCallback(() => {
    if (stateRef.current.status === 'committing' || commitInFlightRef.current) {
      notify?.({ tone: 'info', title: '수동 객체를 저장하고 있습니다.' })
      return
    }
    if (templateId === 'SIGN_SUPPORT_POLE') {
      if (!targetLayer) {
        notify?.({ tone: 'info', title: 'Point 대상 레이어를 먼저 선택해 주세요.' })
        return
      }
      cancel()
      warnIfReviewTaskWillNotLink()
      const initialTemplateOptions = {
        ...poleTemplateOptions,
        allowNearDuplicate: false,
        overrideReason: '',
      }
      overlay.beginStagedPointCreate(targetLayer.id, continuous, initialTemplateOptions)
      window.dispatchEvent(new CustomEvent('mms-open-pointcloud'))
      return
    }
    beginTrafficSignBbox()
  }, [beginTrafficSignBbox, cancel, continuous, notify, overlay, poleTemplateOptions, targetLayer, templateId, warnIfReviewTaskWillNotLink])

  const confirmProposal = useCallback(
    async (nextTask = false) => {
      const current = stateRef.current
      if (current.status !== 'ready') return
      const { data } = current
      const layer = overlay.layers.find((candidate) => candidate.id === data.targetLayerId)
      const linkedTaskSnapshot = data.reviewTaskSnapshot
      if (
        linkedTaskSnapshot &&
        !reviewTaskMatchesSnapshot(
          reviewRef.current?.currentTask,
          reviewRef.current?.session?.id,
          linkedTaskSnapshot,
        )
      ) {
        notify?.({
          tone: 'info',
          title: '검수 항목이 바뀌어 저장을 중단했습니다.',
          message: '이 bbox는 작업 시작 당시 항목에 연결되어 있습니다. bbox 수정으로 다시 그리거나 취소한 뒤 현재 항목에서 시작해 주세요.',
        })
        return
      }
      const linkedTaskId = linkedTaskSnapshot?.id
      if (!layer || missingRequiredFields.length > 0) {
        notify?.({
          tone: 'info',
          title: '필수 속성을 입력해 주세요.',
          message: missingRequiredFields.join(', ') || undefined,
        })
        return
      }
      if (data.duplicate.blocked) {
        notify?.({ tone: 'error', title: '동일 객체가 이미 있어 저장할 수 없습니다.' })
        return
      }
      if (
        data.duplicate.warning_count > 0 &&
        (!allowNearDuplicate || duplicateOverrideReason.trim().length < 3)
      ) {
        notify?.({ tone: 'info', title: '근접 중복 저장 사유를 3자 이상 입력해 주세요.' })
        return
      }
      abortRequest()
      const requestId = requestIdRef.current + 1
      requestIdRef.current = requestId
      const controller = new AbortController()
      requestRef.current = { id: requestId, controller }
      commitInFlightRef.current = true
      const expectedScope = `${datasetId}:${data.frameId}`
      const committingState: ManualProposalState = { status: 'committing', data }
      stateRef.current = committingState
      setProposalState(committingState)
      const currentRevision =
        overlay.features[layer.id]?.dataset?.revision ??
        overlay.features[layer.id]?.wgs84?.revision ??
        layer.revision
      try {
        const response = await api.commitManualObjectProposal(
          data.proposal.proposal_id,
          {
            expected_revision: currentRevision,
            idempotency_key: data.idempotencyKey,
            ...(linkedTaskId ? { task_id: linkedTaskId } : {}),
            created_by: 'operator-local',
            properties: effectiveProperties,
            allow_near_duplicate: allowNearDuplicate,
            ...(data.duplicate.warning_count > 0
              ? { override_reason: duplicateOverrideReason.trim() }
              : {}),
          },
          controller.signal,
        )
        if (!mountedRef.current) return
        if (
          controller.signal.aborted ||
          requestIdRef.current !== requestId ||
          scopeRef.current !== expectedScope
        ) {
          void overlay.refresh()
          reviewRef.current?.reload()
          return
        }
        await overlay.refresh()
        overlay.selectFeature(
          { layerId: data.targetLayerId, featureId: response.feature.id },
          { navigate: false },
        )
        let taskResolutionPending = response.task_resolution_pending
        if (taskResolutionPending && linkedTaskId) {
          try {
            const featureId = String(response.feature.id)
            const reconciled = await api.resolveReviewTask(linkedTaskId, {
              resolution: 'manual_added',
              resolved_feature_ids: [featureId],
            })
            taskResolutionPending = !(
              reconciled.task.status === 'manual_added' &&
              reconciled.task.resolved_feature_ids.some(
                (resolvedFeatureId) => String(resolvedFeatureId) === featureId,
              )
            )
          } catch {
            // The feature already exists; retain the non-retryable warning path.
          }
        }
        reviewRef.current?.reload()
        if (taskResolutionPending) {
          notify?.({
            tone: 'error',
            title: '객체는 저장됐지만 검수 항목 동기화가 필요합니다',
            message: '현재 task를 유지하고 목록을 새로고침했습니다. 동기화 상태를 확인한 뒤 다시 처리해 주세요.',
          })
          setAllowNearDuplicate(false)
          setDuplicateOverrideReason('')
          setBboxMode(false)
          setProposalState({ status: 'idle' })
          return
        }
        const movedToNextTask = Boolean(
          nextTask &&
          linkedTaskSnapshot &&
          reviewTaskHasSnapshotScope(
            reviewRef.current?.currentTask,
            reviewRef.current?.session?.id,
            linkedTaskSnapshot,
          ),
        )
        if (movedToNextTask) reviewRef.current?.moveTask(1)
        notify?.({
          tone: 'success',
          title: movedToNextTask ? '수동 객체를 저장하고 다음 항목으로 이동했습니다' : '수동 객체를 저장했습니다',
        })
        setAllowNearDuplicate(false)
        setDuplicateOverrideReason('')
        bboxReviewTaskSnapshotRef.current = null
        if (continuous && frame && !nextTask) {
          setBboxMode(true)
          setProposalState({ status: 'drawing', frameId: frame.id })
        } else {
          setBboxMode(false)
          setProposalState({ status: 'idle' })
        }
      } catch (reason) {
        if (controller.signal.aborted || requestIdRef.current !== requestId) return
        if (scopeRef.current !== expectedScope || !mountedRef.current) {
          void overlay.refresh()
          reviewRef.current?.reload()
          return
        }
        const message = reason instanceof Error ? reason.message : '수동 객체를 저장하지 못했습니다.'
        if (reason instanceof ApiError && reason.status === 409) await overlay.refresh()
        setProposalState({ status: 'ready', data: { ...data, saveError: message } })
        notify?.({
          tone: 'error',
          title: reason instanceof ApiError && reason.status === 409
            ? '레이어 최신 내용을 불러왔습니다. 제안을 다시 저장해 주세요.'
            : '수동 객체를 저장하지 못했습니다',
          message,
        })
      } finally {
        commitInFlightRef.current = false
        if (requestRef.current?.id === requestId) requestRef.current = null
      }
    },
    [abortRequest, allowNearDuplicate, continuous, datasetId, duplicateOverrideReason, effectiveProperties, frame, missingRequiredFields, notify, overlay],
  )

  const shortcutRef = useRef({
    enabled,
    bboxMode,
    beginTrafficSignBbox,
    cancel,
    confirmProposal,
    overlay,
    review,
    startSelectedTemplate,
  })
  shortcutRef.current = {
    enabled,
    bboxMode,
    beginTrafficSignBbox,
    cancel,
    confirmProposal,
    overlay,
    review,
    startSelectedTemplate,
  }
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        hasOpenModalDialog() ||
        event.defaultPrevented ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        isWorkspaceShortcutBlockedTarget(event.target) ||
        !shortcutRef.current.enabled
      ) return
      const current = stateRef.current
      if (event.code === 'KeyM' && !event.shiftKey) {
        event.preventDefault()
        if (shortcutRef.current.bboxMode || current.status !== 'idle') shortcutRef.current.cancel()
        else shortcutRef.current.beginTrafficSignBbox()
      } else if (event.key === 'Escape' && (shortcutRef.current.bboxMode || current.status !== 'idle')) {
        event.preventDefault()
        shortcutRef.current.cancel()
      } else if (event.key === 'Enter' && event.shiftKey) {
        if (current.status === 'ready') {
          event.preventDefault()
          void shortcutRef.current.confirmProposal(true)
        } else if (shortcutRef.current.overlay.poleBaseProposal.status === 'ready') {
          event.preventDefault()
          void shortcutRef.current.overlay.confirmPoleBaseProposal(true)
        }
      } else if (event.key === 'Enter' && !event.shiftKey && current.status === 'ready') {
        event.preventDefault()
        void shortcutRef.current.confirmProposal(false)
      }
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [])

  const proposal = proposalFromState(proposalState)
  const proposalPosition =
    proposal?.geometry?.type === 'Point' ? proposal.geometry.coordinates : null
  const readyReviewTaskSnapshot =
    proposalState.status === 'ready' || proposalState.status === 'committing'
      ? proposalState.data.reviewTaskSnapshot
      : null
  const reviewTaskLinkChanged = Boolean(
    readyReviewTaskSnapshot &&
    !reviewTaskMatchesSnapshot(
      reviewRef.current?.currentTask,
      reviewRef.current?.session?.id,
      readyReviewTaskSnapshot,
    ),
  )
  const value = useMemo<ManualObjectContextValue>(
    () => ({
      enabled,
      frame,
      templates,
      templatesLoading,
      templateId,
      template,
      setTemplateId,
      pointLayers,
      targetLayerId,
      targetLayer,
      setTargetLayerId,
      properties,
      effectiveProperties,
      setProperty,
      missingRequiredFields,
      continuous,
      setContinuous,
      bboxMode,
      proposalState,
      proposalPosition,
      allowNearDuplicate,
      setAllowNearDuplicate,
      duplicateOverrideReason,
      setDuplicateOverrideReason,
      startSelectedTemplate,
      beginTrafficSignBbox,
      stageBbox,
      submitStagedBbox,
      submitBbox,
      retryProposal,
      retryBbox,
      cancel,
      confirmProposal,
      canConfirmAndNext: Boolean(readyReviewTaskSnapshot) && !reviewTaskLinkChanged,
      reviewTaskLinkChanged,
    }),
    [allowNearDuplicate, bboxMode, beginTrafficSignBbox, cancel, confirmProposal, continuous, duplicateOverrideReason, effectiveProperties, enabled, frame, missingRequiredFields, pointLayers, properties, proposalPosition, proposalState, readyReviewTaskSnapshot, retryBbox, retryProposal, reviewTaskLinkChanged, setContinuous, setProperty, setTargetLayerId, setTemplateId, stageBbox, startSelectedTemplate, submitBbox, submitStagedBbox, targetLayer, targetLayerId, template, templateId, templates, templatesLoading],
  )

  return <ManualObjectContext.Provider value={value}>{children}</ManualObjectContext.Provider>
}

export function useManualObjectWorkspace(): ManualObjectContextValue {
  const value = useContext(ManualObjectContext)
  if (!value) throw new Error('useManualObjectWorkspace must be used inside ManualObjectProvider.')
  return value
}

export function useOptionalManualObjectWorkspace(): ManualObjectContextValue | null {
  return useContext(ManualObjectContext)
}
