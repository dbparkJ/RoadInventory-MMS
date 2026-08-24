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
import { isTextEntryTarget } from '../lib/frameNavigation'
import type {
  OverlayCoordinateSpace,
  OverlayEncoding,
  OverlayFeature,
  OverlayFeatureCollection,
  OverlayFeatureCreateRequest,
  OverlayField,
  OverlayLayer,
  ManualDuplicatePreflightResponse,
  OverlayManualObjectValidation,
  OverlayReviewMetadata,
  PoleBaseInferResponse,
} from '../types'
import { useOptionalReviewWorkspace } from './ReviewContext'

const LAYER_COLORS = ['#2bcfa8', '#ffb84d', '#65a9ff', '#ff6f91', '#b38cff', '#f4e04d']
const FEATURE_PAGE_SIZE = 3_000

function featureMutationKey(prefix: string): string {
  const random =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}_${Math.random().toString(16).slice(2)}`
  return `${prefix}-${random}`
}

interface LayerFeatures {
  wgs84: OverlayFeatureCollection | null
  dataset: OverlayFeatureCollection | null
  loading: boolean
  loadingWgs84?: boolean
  loadingDataset?: boolean
  error?: string
  errorWgs84?: string
  errorDataset?: string
}

interface SelectedFeatureDetailCache {
  datasetId: string
  layerId: string
  featureId: string | number
  revision: number
  feature: OverlayFeature
}

export interface OverlaySelection {
  layerId: string
  featureId: string | number
}

export interface OverlaySelectionOptions {
  /**
   * Whether selecting the feature should also locate and open its MMS frame.
   * Attribute-only actions such as the hover tooltip's Details button disable
   * this so opening the editor cannot reset every viewer.
   */
  navigate?: boolean
}

export type OverlayPickTarget =
  | { kind: 'move'; layerId: string; featureId: string | number }
  | { kind: 'create'; layerId: string }
  | PoleBaseTarget

type DirectPointPickTarget = Extract<OverlayPickTarget, { kind: 'move' | 'create' }>

interface StagedPointWorkflow {
  target: DirectPointPickTarget
  continuous: boolean
  templateOptions?: PoleBaseTemplateOptions
}

interface StagedPointCloudCoordinate extends StagedPointWorkflow {
  frameId: string
  coordinates: [number, number, number]
}

function directPointTargetsMatch(
  left: DirectPointPickTarget,
  right: DirectPointPickTarget,
): boolean {
  if (left.kind !== right.kind || left.layerId !== right.layerId) return false
  if (left.kind === 'create') return true
  return right.kind === 'move' && String(left.featureId) === String(right.featureId)
}

function stagedWorkflowMatchesPoleTarget(
  workflow: StagedPointWorkflow,
  target: PoleBaseTarget,
): boolean {
  if (workflow.target.layerId !== target.layerId) return false
  if (workflow.target.kind === 'create') return target.kind === 'pole-base-create'
  return (
    target.kind === 'pole-base-move' &&
    String(workflow.target.featureId) === String(target.featureId)
  )
}

export type PoleBaseTarget =
  | { kind: 'pole-base-create'; layerId: string; continuous: boolean }
  | { kind: 'pole-base-move'; layerId: string; featureId: string | number }

export interface PoleBaseTemplateOptions {
  templateId: 'SIGN_SUPPORT_POLE'
  properties: Record<string, unknown>
  requiredFields: string[]
  allowNearDuplicate: boolean
  overrideReason: string
}

export interface PoleBaseTemplateValidation {
  duplicate: ManualDuplicatePreflightResponse
  missingRequiredFields: string[]
  allowNearDuplicate: boolean
  overrideReason: string
}

export type PoleBaseProposalState =
  | { status: 'idle' }
  | { status: 'picking'; target: PoleBaseTarget }
  | {
      status: 'loading'
      target: PoleBaseTarget
      frameId: string
      seed: [number, number, number]
    }
  | {
      status: 'ready'
      target: PoleBaseTarget
      frameId: string
      seed: [number, number, number]
      result: PoleBaseInferResponse
      idempotencyKey: string
      templateValidation?: PoleBaseTemplateValidation
    }
  | {
      status: 'error'
      target: PoleBaseTarget
      frameId?: string
      seed?: [number, number, number]
      message: string
      reasonCodes: string[]
    }

export const POLE_BASE_REASON_MESSAGES: Readonly<Record<string, string>> = {
  CATALOG_PREPARING: '원본 점군을 준비하고 있습니다. 잠시 후 다시 선택해 주세요.',
  INVALID_SEED: '선택한 지주점 좌표가 올바르지 않습니다.',
  METRIC_CRS_REQUIRED: '미터 단위 좌표계가 필요한 데이터셋입니다.',
  SEED_OUTSIDE_FRAME_WINDOW: '선택한 점이 현재 프레임의 작업 범위를 벗어났습니다.',
  SEED_NOT_ON_SOURCE_POINT: '선택한 점 주변에서 원본 점군을 찾지 못했습니다.',
  NO_LOCAL_POINTS: '선택한 점 주변에 분석할 원본 점이 없습니다.',
  LOCAL_POINT_LIMIT_EXCEEDED: '주변 점이 너무 많아 안전 제한을 초과했습니다.',
  TOO_MANY_CANDIDATE_BLOCKS: '조회할 점군 블록이 너무 많습니다.',
  NO_VERTICAL_AXIS: '지주로 판단할 수 있는 수직 축을 찾지 못했습니다.',
  AXIS_TOO_SHORT: '검출된 지주 축의 길이가 너무 짧습니다.',
  AXIS_DISCONTINUOUS: '지주 축의 점 분포가 연속적이지 않습니다.',
  AXIS_RMSE_HIGH: '지주 축 맞춤 오차가 큽니다.',
  AXIS_TILT_EXCESS: '검출된 축의 기울기가 지주 허용 범위를 벗어났습니다.',
  AMBIGUOUS_AXES: '서로 비슷한 지주 후보가 여러 개 있어 확인이 필요합니다.',
  NO_GROUND_SUPPORT: '지주 주변에서 지면을 찾지 못했습니다.',
  GROUND_RMSE_HIGH: '지면 맞춤 오차가 큽니다.',
  GROUND_HYPOTHESES_CONFLICT: '서로 다른 지면 후보가 충돌합니다.',
  GROUND_TOO_FAR: '신뢰할 수 있는 지면 점이 지주에서 너무 멉니다.',
  GROUND_PENETRATION: '검출된 지주가 추정 지면 아래로 지나치게 들어갑니다.',
  BOTTOM_EXTRAPOLATED: '관측된 지주 끝에서 바닥까지 외삽 거리가 깁니다.',
  BASE_OUTSIDE_LOCAL_WINDOW: '추정 바닥점이 분석 범위를 벗어났습니다.',
  TASK_RESOLUTION_PENDING: '객체는 저장됐지만 검수 항목 동기화가 필요합니다.',
  DUPLICATE_EXACT: '동일한 지주 객체가 이미 존재합니다.',
  DUPLICATE_NEARBY: '가까운 지주 객체가 있어 저장 확인이 필요합니다.',
}

export function poleBaseReasonMessage(reasonCode: string): string {
  return POLE_BASE_REASON_MESSAGES[reasonCode] ?? reasonCode
}

export function poleBaseReasonMessages(reasonCodes: readonly string[]): string[] {
  return [...new Set(reasonCodes)].map(poleBaseReasonMessage)
}

function poleBaseReasonCodesFromUnknown(value: unknown): string[] {
  const found: string[] = []
  const visit = (candidate: unknown, depth: number) => {
    if (!candidate || typeof candidate !== 'object' || depth > 3) return
    const record = candidate as Record<string, unknown>
    const reasonCodes = record.reason_codes ?? record.reasonCodes
    if (Array.isArray(reasonCodes)) {
      reasonCodes.forEach((reasonCode) => {
        if (typeof reasonCode === 'string' && reasonCode) found.push(reasonCode)
      })
    }
    if (
      typeof record.code === 'string' &&
      Object.prototype.hasOwnProperty.call(POLE_BASE_REASON_MESSAGES, record.code)
    ) {
      found.push(record.code)
    }
    visit(record.detail, depth + 1)
    visit(record.details, depth + 1)
  }
  visit(value, 0)
  return [...new Set(found)]
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === 'AbortError'
}

const POLE_BASE_FIELD_ALIASES: Record<
  'x' | 'y' | 'z' | 'method' | 'quality' | 'status' | 'frame',
  ReadonlySet<string>
> = {
  x: new Set(['BASE_X', 'BAS_X', 'POLE_X']),
  y: new Set(['BASE_Y', 'BAS_Y', 'POLE_Y']),
  z: new Set(['BASE_Z', 'BAS_Z', 'POLE_Z', 'ELEV']),
  method: new Set(['BASE_MTH', 'BAS_MTH']),
  quality: new Set(['BASE_Q', 'BAS_Q']),
  status: new Set(['BASE_ST', 'BAS_ST', 'QA_STATUS']),
  frame: new Set(['SRC_FRAME', 'FRAME_ID']),
}

function normalizedPoleBaseFieldName(name: string): string {
  return name.trim().toUpperCase().replace(/\s+/g, '_')
}

function normalizedOverlayFieldType(field: OverlayField): string {
  return (field.type ?? '').trim().toUpperCase()
}

function isNumericOverlayField(field: OverlayField): boolean {
  return new Set([
    'N',
    'F',
    'I',
    'B',
    'O',
    'NUMBER',
    'NUMERIC',
    'DECIMAL',
    'FLOAT',
    'DOUBLE',
    'INTEGER',
    'INT',
  ]).has(normalizedOverlayFieldType(field))
}

function isIntegerOverlayField(field: OverlayField): boolean {
  const type = normalizedOverlayFieldType(field)
  return (
    type === 'I' ||
    type === 'INTEGER' ||
    type === 'INT' ||
    ((type === 'N' || type === 'F') && field.decimal === 0)
  )
}

function poleBaseFieldValue(
  field: OverlayField,
  value: number | string,
  quality = false,
): number | string {
  if (!isNumericOverlayField(field)) return String(value)
  const numericValue = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numericValue)) return String(value)
  if (quality && isIntegerOverlayField(field)) return Math.round(numericValue * 100)
  if (isIntegerOverlayField(field)) return Math.round(numericValue)
  return numericValue
}

export function buildPoleBasePropertyPatch(
  fields: OverlayField[],
  currentProperties: Record<string, unknown>,
  result: PoleBaseInferResponse,
  frameId: string,
): Record<string, unknown> {
  if (!result.base_position || result.status === 'failed') return {}
  const [baseX, baseY, baseZ] = result.base_position
  const sourceValues = {
    x: baseX,
    y: baseY,
    z: baseZ,
    method: 'MAN_SEED',
    quality: result.quality.score,
    status: result.status === 'review' ? 'REVIEW' : 'AUTO',
    frame: frameId,
  } as const
  const patch: Record<string, unknown> = {}
  fields.forEach((field) => {
    if (!field.name || field.internal) return
    const normalizedName = normalizedPoleBaseFieldName(field.name)
    const semantic = (Object.keys(POLE_BASE_FIELD_ALIASES) as Array<keyof typeof POLE_BASE_FIELD_ALIASES>)
      .find((candidate) => POLE_BASE_FIELD_ALIASES[candidate].has(normalizedName))
    if (!semantic) return
    const value = poleBaseFieldValue(field, sourceValues[semantic], semantic === 'quality')
    if (!Object.is(currentProperties[field.name], value)) patch[field.name] = value
  })
  return patch
}

function hasPoleTemplatePropertyValue(value: unknown): boolean {
  return value !== null && value !== undefined &&
    (typeof value !== 'string' || value.trim().length > 0)
}

function buildPoleBaseTemplatePropertyPatch(
  fields: OverlayField[],
  currentProperties: Record<string, unknown>,
  result: PoleBaseInferResponse,
  frameId: string,
  options?: PoleBaseTemplateOptions,
): { properties: Record<string, unknown>; missingRequiredFields: string[] } {
  const automatic = buildPoleBasePropertyPatch(fields, currentProperties, result, frameId)
  const properties = options ? { ...options.properties, ...automatic } : automatic
  const finalProperties = { ...currentProperties, ...properties }
  return {
    properties,
    missingRequiredFields: options
      ? options.requiredFields.filter((fieldName) =>
          !hasPoleTemplatePropertyValue(finalProperties[fieldName]),
        )
      : [],
  }
}

export function poleBaseTemplateValidationBlocksSave(
  validation: PoleBaseTemplateValidation | undefined,
): boolean {
  if (!validation) return false
  return validation.duplicate.exact_duplicate ||
    validation.missingRequiredFields.length > 0 ||
    (validation.duplicate.warning_count > 0 &&
      (!validation.allowNearDuplicate || validation.overrideReason.trim().length < 3))
}

export interface OverlayContextValue {
  datasetId: string
  poleBaseInferenceEnabled: boolean
  layers: OverlayLayer[]
  features: Record<string, LayerFeatures>
  visibleLayerIds: Set<string>
  activeLayerId: string
  setActiveLayerId: (layerId: string) => void
  selected: OverlaySelection | null
  selectedLayer: OverlayLayer | null
  selectedFeature: OverlayFeature | null
  selectedDatasetFeature: OverlayFeature | null
  mapFeatures: OverlayFeature[]
  datasetFeatures: Array<{ layerId: string; color: string; feature: OverlayFeature }>
  loading: boolean
  uploading: boolean
  creatingFeature: boolean
  pickMode: boolean
  pickTarget: OverlayPickTarget | null
  poleBaseProposal: PoleBaseProposalState
  setPickMode: (enabled: boolean) => void
  beginCreatePoint: (layerId: string) => void
  beginStagedPointCreate: (
    layerId: string,
    continuous?: boolean,
    templateOptions?: PoleBaseTemplateOptions,
  ) => void
  beginStagedSelectedPointMove: () => void
  updateStagedPoleBaseTemplateOptions: (
    layerId: string,
    templateOptions: PoleBaseTemplateOptions,
  ) => void
  beginCreatePoleBase: (layerId: string, continuous?: boolean) => void
  beginRecomputeSelectedPoleBase: () => void
  applyPoleSeed: (frameId: string, coordinates: [number, number, number]) => Promise<void>
  confirmPoleBaseProposal: () => Promise<boolean>
  retryPoleBasePick: () => void
  cancelPoleBaseProposal: () => void
  handlePoleBaseFrameChange: (frameId: string) => void
  refresh: () => Promise<void>
  ensureDatasetFeatures: (layerId: string) => Promise<void>
  loadMoreDatasetFeatures: (layerId: string) => Promise<void>
  upload: (files: File[], name?: string, crs?: string, encoding?: OverlayEncoding) => Promise<void>
  updateLayerMetadata: (layerId: string, patch: { name?: string; color?: string }) => Promise<void>
  removeLayer: (layerId: string) => Promise<void>
  toggleLayer: (layerId: string) => void
  selectFeature: (
    selection: OverlaySelection | null,
    options?: OverlaySelectionOptions,
  ) => void
  updateSelected: (patch: {
    geometry?: { type: 'Point'; coordinates: [number, number, number?] }
    coordinate_space?: OverlayCoordinateSpace
    properties?: Record<string, unknown>
    review_metadata?: OverlayReviewMetadata
    manual_object_validation?: OverlayManualObjectValidation
  }) => Promise<void>
  applyPickedCoordinate: (
    coordinates: [number, number, number?],
    coordinateSpace: OverlayCoordinateSpace,
  ) => Promise<void>
  applyPointCloudCoordinate: (
    frameId: string,
    coordinates: [number, number, number],
  ) => Promise<void>
  copySelectedLocation: () => Promise<void>
  deleteSelected: () => Promise<void>
  deleteField: (layerId: string, fieldName: string) => Promise<void>
  layerColor: (layerId: string) => string
}

const OverlayContext = createContext<OverlayContextValue | null>(null)

function featureId(feature: OverlayFeature): string {
  return String(feature.id)
}

function detailFeatureForSelection(
  detail: SelectedFeatureDetailCache | null,
  datasetId: string,
  selection: OverlaySelection | null,
  layerRevision: number | undefined,
): OverlayFeature | null {
  if (
    !detail ||
    !selection ||
    layerRevision === undefined ||
    detail.datasetId !== datasetId ||
    detail.layerId !== selection.layerId ||
    String(detail.featureId) !== String(selection.featureId) ||
    detail.revision < layerRevision
  ) {
    return null
  }
  return detail.feature
}

function emptyCollection(revision = 0): OverlayFeatureCollection {
  return {
    type: 'FeatureCollection',
    features: [],
    fields: [],
    total: 0,
    offset: 0,
    limit: 0,
    revision,
  }
}

export function OverlayProvider({
  datasetId,
  activeFrameId,
  poleBaseInferenceEnabled = true,
  demoMode,
  notify,
  children,
}: {
  datasetId: string
  activeFrameId?: string | null
  poleBaseInferenceEnabled?: boolean
  demoMode: boolean
  notify?: (entry: { tone: 'success' | 'error' | 'info'; title: string; message?: string }) => void
  children: ReactNode
}) {
  const review = useOptionalReviewWorkspace()
  const [layers, setLayers] = useState<OverlayLayer[]>([])
  const [features, setFeatures] = useState<Record<string, LayerFeatures>>({})
  const [visibleLayerIds, setVisibleLayerIds] = useState<Set<string>>(new Set())
  const [activeLayerId, setActiveLayerId] = useState('')
  const [selected, setSelected] = useState<OverlaySelection | null>(null)
  const [selectedDatasetDetail, setSelectedDatasetDetail] =
    useState<SelectedFeatureDetailCache | null>(null)
  const [selectedWgs84Detail, setSelectedWgs84Detail] =
    useState<SelectedFeatureDetailCache | null>(null)
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [creatingFeature, setCreatingFeature] = useState(false)
  const [pickTarget, setPickTarget] = useState<OverlayPickTarget | null>(null)
  const [poleBaseProposal, setPoleBaseProposal] = useState<PoleBaseProposalState>({ status: 'idle' })
  const pickMode = pickTarget !== null
  const requestGeneration = useRef(0)
  const refreshControllerRef = useRef<AbortController | null>(null)
  const loadedDatasetRef = useRef('')
  const knownLayerIdsRef = useRef<Set<string>>(new Set())
  const coordinateLoadsRef = useRef<Set<string>>(new Set())
  const selectionRequestRef = useRef(0)
  const selectionControllerRef = useRef<AbortController | null>(null)
  const poleBaseProposalRef = useRef<PoleBaseProposalState>({ status: 'idle' })
  const poleBaseRequestRef = useRef<{ requestId: number; controller: AbortController } | null>(null)
  const poleBaseRequestIdRef = useRef(0)
  const poleBaseSaveIdRef = useRef(0)
  const poleBaseConfirmingRef = useRef(false)
  const featureTaskResolutionPendingRef = useRef<boolean | null>(null)
  const savedFeatureIdRef = useRef<string | number | null>(null)
  const stagedPointWorkflowRef = useRef<StagedPointWorkflow | null>(null)
  const stagedPointCloudCoordinateRef = useRef<StagedPointCloudCoordinate | null>(null)
  const directActualPointPickRef = useRef(false)
  const activeDatasetIdRef = useRef(datasetId)
  activeDatasetIdRef.current = datasetId
  const visibleLayerIdsRef = useRef(visibleLayerIds)
  visibleLayerIdsRef.current = visibleLayerIds

  const commitPoleBaseProposal = useCallback((proposal: PoleBaseProposalState) => {
    poleBaseProposalRef.current = proposal
    setPoleBaseProposal(proposal)
  }, [])

  const abortPoleBaseRequest = useCallback(() => {
    poleBaseRequestIdRef.current += 1
    poleBaseRequestRef.current?.controller.abort()
    poleBaseRequestRef.current = null
  }, [])

  const restoreStagedPointWorkflow = useCallback(
    (workflow: StagedPointWorkflow) => {
      stagedPointWorkflowRef.current = workflow
      stagedPointCloudCoordinateRef.current = null
      directActualPointPickRef.current = false
      setActiveLayerId(workflow.target.layerId)
      setPickTarget(workflow.target)
      commitPoleBaseProposal({ status: 'idle' })
    },
    [commitPoleBaseProposal],
  )

  const clearPoleBaseWorkflow = useCallback(() => {
    abortPoleBaseRequest()
    poleBaseSaveIdRef.current += 1
    poleBaseConfirmingRef.current = false
    stagedPointWorkflowRef.current = null
    stagedPointCloudCoordinateRef.current = null
    directActualPointPickRef.current = false
    commitPoleBaseProposal({ status: 'idle' })
    setPickTarget((current) =>
      current?.kind === 'pole-base-create' || current?.kind === 'pole-base-move' ? null : current,
    )
  }, [abortPoleBaseRequest, commitPoleBaseProposal])

  const clearSelectedDetails = useCallback(() => {
    selectionRequestRef.current += 1
    selectionControllerRef.current?.abort()
    selectionControllerRef.current = null
    setSelectedDatasetDetail(null)
    setSelectedWgs84Detail(null)
  }, [])

  const layerColor = useCallback(
    (layerId: string) => {
      const savedColor = layers.find((layer) => layer.id === layerId)?.color
      if (savedColor) return savedColor
      let hash = 0
      for (let index = 0; index < layerId.length; index += 1) {
        hash = (hash * 31 + layerId.charCodeAt(index)) | 0
      }
      return LAYER_COLORS[Math.abs(hash) % LAYER_COLORS.length]
    },
    [layers],
  )

  const setPickMode = useCallback(
    (enabled: boolean) => {
      if (!enabled) {
        stagedPointWorkflowRef.current = null
        stagedPointCloudCoordinateRef.current = null
        directActualPointPickRef.current = false
        setPickTarget(null)
        if (poleBaseProposalRef.current.status !== 'idle') clearPoleBaseWorkflow()
        return
      }
      if (!selected) {
        notify?.({ tone: 'info', title: '먼저 위치를 수정할 SHP 피처를 선택해 주세요.' })
        return
      }
      if (poleBaseProposalRef.current.status !== 'idle') clearPoleBaseWorkflow()
      stagedPointWorkflowRef.current = null
      stagedPointCloudCoordinateRef.current = null
      directActualPointPickRef.current = true
      setPickTarget({
        kind: 'move',
        layerId: selected.layerId,
        featureId: selected.featureId,
      })
    },
    [clearPoleBaseWorkflow, notify, selected],
  )

  const beginCreatePoint = useCallback(
    (layerId: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!layer) {
        notify?.({ tone: 'error', title: '신규 피처를 추가할 SHP 레이어가 없습니다.' })
        return
      }
      if (demoMode) {
        notify?.({ tone: 'info', title: '데모 모드에서는 SHP 피처를 추가할 수 없습니다.' })
        return
      }
      if (layer.geometry_type !== 'Point') {
        notify?.({ tone: 'info', title: '지도 클릭 신규 추가는 Point 레이어에서만 사용할 수 있습니다.' })
        return
      }
      if (poleBaseProposalRef.current.status !== 'idle') clearPoleBaseWorkflow()
      stagedPointWorkflowRef.current = null
      stagedPointCloudCoordinateRef.current = null
      directActualPointPickRef.current = true
      setActiveLayerId(layerId)
      setPickTarget({ kind: 'create', layerId })
    },
    [clearPoleBaseWorkflow, demoMode, layers, notify],
  )

  const beginStagedPointCreate = useCallback(
    (
      layerId: string,
      continuous = false,
      templateOptions?: PoleBaseTemplateOptions,
    ) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      beginCreatePoint(layerId)
      if (!layer || layer.geometry_type !== 'Point' || demoMode) return
      directActualPointPickRef.current = false
      stagedPointWorkflowRef.current = {
        target: { kind: 'create', layerId },
        continuous,
        templateOptions,
      }
    },
    [beginCreatePoint, demoMode, layers],
  )

  const beginStagedSelectedPointMove = useCallback(() => {
    setPickMode(true)
    if (!selected) return
    directActualPointPickRef.current = false
    stagedPointWorkflowRef.current = {
      target: {
        kind: 'move',
        layerId: selected.layerId,
        featureId: selected.featureId,
      },
      continuous: false,
    }
  }, [selected, setPickMode])

  const updateStagedPoleBaseTemplateOptions = useCallback(
    (layerId: string, templateOptions: PoleBaseTemplateOptions) => {
      const workflow = stagedPointWorkflowRef.current
      if (!workflow || workflow.target.layerId !== layerId) return
      const nextWorkflow = { ...workflow, templateOptions }
      stagedPointWorkflowRef.current = nextWorkflow
      const staged = stagedPointCloudCoordinateRef.current
      if (staged && directPointTargetsMatch(staged.target, workflow.target)) {
        stagedPointCloudCoordinateRef.current = { ...staged, templateOptions }
      }
      const proposal = poleBaseProposalRef.current
      if (
        proposal.status !== 'ready' ||
        !proposal.templateValidation ||
        !stagedWorkflowMatchesPoleTarget(nextWorkflow, proposal.target)
      ) return
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!layer) return
      const fields =
        layer.fields ??
        features[layerId]?.dataset?.fields ??
        features[layerId]?.wgs84?.fields ??
        []
      const moveFeatureId =
        proposal.target.kind === 'pole-base-move'
          ? String(proposal.target.featureId)
          : null
      const currentProperties =
        moveFeatureId !== null
          ? (
              features[layerId]?.dataset?.features.find(
                (feature) => String(feature.id) === moveFeatureId,
              )?.properties ??
              selectedDatasetDetail?.feature.properties ??
              features[layerId]?.wgs84?.features.find(
                (feature) => String(feature.id) === moveFeatureId,
              )?.properties ??
              selectedWgs84Detail?.feature.properties ??
              {}
            )
          : {}
      const propertyResult = buildPoleBaseTemplatePropertyPatch(
        fields,
        currentProperties,
        proposal.result,
        proposal.frameId,
        templateOptions,
      )
      commitPoleBaseProposal({
        ...proposal,
        templateValidation: {
          ...proposal.templateValidation,
          missingRequiredFields: propertyResult.missingRequiredFields,
          allowNearDuplicate: templateOptions.allowNearDuplicate,
          overrideReason: templateOptions.overrideReason,
        },
      })
    },
    [
      commitPoleBaseProposal,
      features,
      layers,
      selectedDatasetDetail,
      selectedWgs84Detail,
    ],
  )

  const beginCreatePoleBase = useCallback(
    (layerId: string, continuous = true) => {
      if (!poleBaseInferenceEnabled) {
        notify?.({ tone: 'info', title: '이 서버에서는 지주 하단 자동 산출을 사용할 수 없습니다.' })
        return
      }
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!layer) {
        notify?.({ tone: 'error', title: '지주를 추가할 SHP 레이어가 없습니다.' })
        return
      }
      if (demoMode) {
        notify?.({ tone: 'info', title: '데모 모드에서는 지주 피처를 추가할 수 없습니다.' })
        return
      }
      if (layer.geometry_type !== 'Point') {
        notify?.({ tone: 'info', title: '지주 하단 자동 산출은 Point 레이어에서만 사용할 수 있습니다.' })
        return
      }
      abortPoleBaseRequest()
      poleBaseSaveIdRef.current += 1
      poleBaseConfirmingRef.current = false
      stagedPointWorkflowRef.current = null
      stagedPointCloudCoordinateRef.current = null
      directActualPointPickRef.current = false
      const target: PoleBaseTarget = { kind: 'pole-base-create', layerId, continuous }
      setActiveLayerId(layerId)
      setPickTarget(target)
      commitPoleBaseProposal({ status: 'picking', target })
      window.dispatchEvent(new CustomEvent('mms-open-pointcloud'))
    },
    [
      abortPoleBaseRequest,
      commitPoleBaseProposal,
      demoMode,
      layers,
      notify,
      poleBaseInferenceEnabled,
    ],
  )

  const beginRecomputeSelectedPoleBase = useCallback(() => {
    if (!poleBaseInferenceEnabled) {
      notify?.({ tone: 'info', title: '이 서버에서는 지주 하단 자동 산출을 사용할 수 없습니다.' })
      return
    }
    const layer = layers.find((candidate) => candidate.id === selected?.layerId)
    if (!selected || !layer) {
      notify?.({ tone: 'info', title: '지주 하단을 재산출할 Point 피처를 먼저 선택해 주세요.' })
      return
    }
    if (demoMode) {
      notify?.({ tone: 'info', title: '데모 모드에서는 지주 위치를 수정할 수 없습니다.' })
      return
    }
    if (layer.geometry_type !== 'Point') {
      notify?.({ tone: 'info', title: '지주 하단 자동 산출은 Point 레이어에서만 사용할 수 있습니다.' })
      return
    }
    abortPoleBaseRequest()
    poleBaseSaveIdRef.current += 1
    poleBaseConfirmingRef.current = false
    stagedPointWorkflowRef.current = null
    stagedPointCloudCoordinateRef.current = null
    directActualPointPickRef.current = false
    const target: PoleBaseTarget = {
      kind: 'pole-base-move',
      layerId: selected.layerId,
      featureId: selected.featureId,
    }
    setActiveLayerId(selected.layerId)
    setPickTarget(target)
    commitPoleBaseProposal({ status: 'picking', target })
    window.dispatchEvent(new CustomEvent('mms-open-pointcloud'))
  }, [
    abortPoleBaseRequest,
    commitPoleBaseProposal,
    demoMode,
    layers,
    notify,
    poleBaseInferenceEnabled,
    selected,
  ])

  const beginDirectActualPointPick = useCallback(
    (target: DirectPointPickTarget) => {
      const layer = layers.find((candidate) => candidate.id === target.layerId)
      if (!layer || layer.geometry_type !== 'Point' || demoMode) return
      if (
        target.kind === 'move' &&
        (!selected ||
          selected.layerId !== target.layerId ||
          String(selected.featureId) !== String(target.featureId))
      ) {
        notify?.({ tone: 'info', title: '먼저 속성표에서 수정할 피처를 선택해 주세요.' })
        return
      }
      if (poleBaseProposalRef.current.status !== 'idle') clearPoleBaseWorkflow()
      stagedPointWorkflowRef.current = null
      stagedPointCloudCoordinateRef.current = null
      directActualPointPickRef.current = true
      setActiveLayerId(target.layerId)
      setPickTarget(target)
    },
    [clearPoleBaseWorkflow, demoMode, layers, notify, selected],
  )

  const retryPoleBasePick = useCallback(() => {
    const proposal = poleBaseProposalRef.current
    if (proposal.status === 'idle') return
    // The feature has already been persisted in this state. Starting another
    // pick would allow a second create/patch while only task reconciliation is
    // pending, so the operator may only dismiss the notice and reload the task.
    if (
      proposal.status === 'error' &&
      proposal.reasonCodes.includes('TASK_RESOLUTION_PENDING')
    ) return
    const workflow = stagedPointWorkflowRef.current
    abortPoleBaseRequest()
    poleBaseSaveIdRef.current += 1
    poleBaseConfirmingRef.current = false
    const target = proposal.target
    if (workflow && stagedWorkflowMatchesPoleTarget(workflow, target)) {
      restoreStagedPointWorkflow(workflow)
      return
    }
    stagedPointWorkflowRef.current = null
    stagedPointCloudCoordinateRef.current = null
    directActualPointPickRef.current = false
    setPickTarget(target)
    commitPoleBaseProposal({ status: 'picking', target })
  }, [abortPoleBaseRequest, commitPoleBaseProposal, restoreStagedPointWorkflow])

  const cancelPoleBaseProposal = useCallback(() => {
    clearPoleBaseWorkflow()
  }, [clearPoleBaseWorkflow])

  const handlePoleBaseFrameChange = useCallback(
    (frameId: string) => {
      const staged = stagedPointCloudCoordinateRef.current
      if (staged && staged.frameId !== frameId) stagedPointCloudCoordinateRef.current = null
      const proposal = poleBaseProposalRef.current
      if (proposal.status === 'idle' || proposal.status === 'picking') return
      if (proposal.frameId === frameId) return
      const workflow = stagedPointWorkflowRef.current
      abortPoleBaseRequest()
      poleBaseSaveIdRef.current += 1
      poleBaseConfirmingRef.current = false
      const target = proposal.target
      if (workflow && stagedWorkflowMatchesPoleTarget(workflow, target)) {
        restoreStagedPointWorkflow(workflow)
        return
      }
      stagedPointWorkflowRef.current = null
      stagedPointCloudCoordinateRef.current = null
      setPickTarget(target)
      commitPoleBaseProposal({ status: 'picking', target })
    },
    [abortPoleBaseRequest, commitPoleBaseProposal, restoreStagedPointWorkflow],
  )

  useEffect(() => {
    if (activeFrameId === undefined) return
    handlePoleBaseFrameChange(activeFrameId ?? '')
  }, [activeFrameId, handlePoleBaseFrameChange])

  useEffect(() => {
    abortPoleBaseRequest()
    poleBaseSaveIdRef.current += 1
    poleBaseConfirmingRef.current = false
    stagedPointWorkflowRef.current = null
    stagedPointCloudCoordinateRef.current = null
    directActualPointPickRef.current = false
    commitPoleBaseProposal({ status: 'idle' })
    setPickTarget((current) =>
      current?.kind === 'pole-base-create' || current?.kind === 'pole-base-move' ? null : current,
    )
    return () => {
      abortPoleBaseRequest()
      poleBaseSaveIdRef.current += 1
    }
  }, [abortPoleBaseRequest, commitPoleBaseProposal, datasetId, demoMode])

  const loadFeaturePage = useCallback(
    async (
      layer: OverlayLayer,
      coordinateSpace: OverlayCoordinateSpace,
      generation: number,
      offset = 0,
      append = false,
      signal?: AbortSignal,
    ) => {
      const loadKey = `${generation}:${layer.id}:${coordinateSpace}:${offset}`
      if (coordinateLoadsRef.current.has(loadKey)) return
      coordinateLoadsRef.current.add(loadKey)
      setFeatures((current) => ({
        ...current,
        [layer.id]: {
          ...(current[layer.id] ?? { wgs84: null, dataset: null }),
          loading: true,
          [coordinateSpace === 'wgs84' ? 'loadingWgs84' : 'loadingDataset']: true,
          error: undefined,
        },
      }))
      try {
        const page = await api.overlayFeatures(
          datasetId,
          layer.id,
          coordinateSpace,
          offset,
          FEATURE_PAGE_SIZE,
          signal,
        )
        if (requestGeneration.current !== generation || signal?.aborted) return
        setFeatures((current) => {
          const currentLayer = current[layer.id] ?? { wgs84: null, dataset: null, loading: false }
          const existingCollection = currentLayer[coordinateSpace]
          let nextCollection = page
          if (append && existingCollection) {
            const existingIds = new Set(existingCollection.features.map((feature) => String(feature.id)))
            nextCollection = {
              ...page,
              offset: 0,
              features: [
                ...existingCollection.features,
                ...page.features.filter((candidate) => !existingIds.has(String(candidate.id))),
              ],
            }
          }
          return {
            ...current,
            [layer.id]: {
              ...currentLayer,
              [coordinateSpace]: nextCollection,
              [coordinateSpace === 'wgs84' ? 'loadingWgs84' : 'loadingDataset']: false,
              loading: Boolean(
                currentLayer[
                  coordinateSpace === 'wgs84' ? 'loadingDataset' : 'loadingWgs84'
                ],
              ),
              [coordinateSpace === 'wgs84' ? 'errorWgs84' : 'errorDataset']: undefined,
              error:
                currentLayer[
                  coordinateSpace === 'wgs84' ? 'errorDataset' : 'errorWgs84'
                ],
            },
          }
        })
        setLayers((current) => {
          let changed = false
          const next = current.map((candidate) => {
            if (
              candidate.id !== layer.id ||
              (candidate.feature_count === page.total && candidate.revision === page.revision)
            ) {
              return candidate
            }
            changed = true
            return { ...candidate, feature_count: page.total, revision: page.revision }
          })
          return changed ? next : current
        })
      } catch (reason) {
        if (signal?.aborted || requestGeneration.current !== generation) return
        setFeatures((current) => {
          const message = reason instanceof Error ? reason.message : 'SHP 피처를 불러오지 못했습니다.'
          return {
            ...current,
            [layer.id]: {
              ...(current[layer.id] ?? {
              wgs84: coordinateSpace === 'wgs84' ? emptyCollection(layer.revision) : null,
              dataset: coordinateSpace === 'dataset' ? emptyCollection(layer.revision) : null,
              }),
              [coordinateSpace === 'wgs84' ? 'loadingWgs84' : 'loadingDataset']: false,
              [coordinateSpace === 'wgs84' ? 'errorWgs84' : 'errorDataset']: message,
              loading: Boolean(
                current[layer.id]?.[
                  coordinateSpace === 'wgs84' ? 'loadingDataset' : 'loadingWgs84'
                ],
              ),
              error: message,
            },
          }
        })
      } finally {
        coordinateLoadsRef.current.delete(loadKey)
      }
    },
    [datasetId],
  )

  const refresh = useCallback(async () => {
    const generation = requestGeneration.current + 1
    requestGeneration.current = generation
    refreshControllerRef.current?.abort()
    if (!datasetId || demoMode) {
      loadedDatasetRef.current = datasetId
      knownLayerIdsRef.current = new Set()
      clearSelectedDetails()
      setLayers([])
      setFeatures({})
      setVisibleLayerIds(new Set())
      setActiveLayerId('')
      setSelected(null)
      setPickTarget(null)
      return
    }
    const controller = new AbortController()
    refreshControllerRef.current = controller
    if (loadedDatasetRef.current !== datasetId) {
      loadedDatasetRef.current = datasetId
      knownLayerIdsRef.current = new Set()
      clearSelectedDetails()
      setLayers([])
      setFeatures({})
      setVisibleLayerIds(new Set())
      setActiveLayerId('')
      setSelected(null)
      setPickTarget(null)
    }
    setLoading(true)
    try {
      const response = await api.overlays(datasetId, controller.signal)
      if (requestGeneration.current !== generation) return
      const nextLayers = response.items ?? []
      const previousLayerIds = knownLayerIdsRef.current
      const nextLayerIds = new Set(nextLayers.map((layer) => layer.id))
      const visibleForLoad = new Set(
        [...visibleLayerIdsRef.current].filter((layerId) => nextLayerIds.has(layerId)),
      )
      nextLayers.forEach((layer) => {
        if (!previousLayerIds.has(layer.id)) visibleForLoad.add(layer.id)
      })
      knownLayerIdsRef.current = nextLayerIds
      setLayers(nextLayers)
      setActiveLayerId((current) =>
        current && nextLayerIds.has(current) ? current : (nextLayers[0]?.id ?? ''),
      )
      setFeatures((current) =>
        Object.fromEntries(
          nextLayers.map((layer) => {
            const cached = current[layer.id]
            const cachedRevision = cached?.dataset?.revision ?? cached?.wgs84?.revision
            return [
              layer.id,
              cached && cachedRevision === layer.revision
                ? cached
                : { wgs84: null, dataset: null, loading: false },
            ]
          }),
        ),
      )
      setVisibleLayerIds(visibleForLoad)
      setSelected((current) =>
        current && nextLayers.some((layer) => layer.id === current.layerId) ? current : null,
      )
      const keepCurrentDetail = (detail: SelectedFeatureDetailCache | null) => {
        if (!detail || detail.datasetId !== datasetId) return null
        const layer = nextLayers.find((candidate) => candidate.id === detail.layerId)
        return layer && detail.revision >= layer.revision ? detail : null
      }
      setSelectedDatasetDetail(keepCurrentDetail)
      setSelectedWgs84Detail(keepCurrentDetail)
      setPickTarget((current) =>
        current && nextLayerIds.has(current.layerId) ? current : null,
      )
      const visibleLayers = nextLayers.filter((layer) => visibleForLoad.has(layer.id))
      let nextLayerIndex = 0
      const loadVisibleMapPages = async () => {
        while (!controller.signal.aborted) {
          const layer = visibleLayers[nextLayerIndex]
          nextLayerIndex += 1
          if (!layer) return
          await loadFeaturePage(layer, 'wgs84', generation, 0, false, controller.signal)
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(4, visibleLayers.length) }, () => loadVisibleMapPages()),
      )
    } catch (reason) {
      if (requestGeneration.current !== generation) return
      notify?.({
        tone: 'error',
        title: 'SHP 레이어를 불러오지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
    } finally {
      if (requestGeneration.current === generation) setLoading(false)
      if (refreshControllerRef.current === controller) refreshControllerRef.current = null
    }
  }, [clearSelectedDetails, datasetId, demoMode, loadFeaturePage, notify])

  useEffect(() => {
    void refresh()
    const onChanged = () => void refresh()
    window.addEventListener('mms-overlay-changed', onChanged)
    return () => {
      window.removeEventListener('mms-overlay-changed', onChanged)
      refreshControllerRef.current?.abort()
    }
  }, [refresh])

  const ensureDatasetFeatures = useCallback(
    async (layerId: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!layer || features[layerId]?.dataset || features[layerId]?.loadingDataset) return
      await loadFeaturePage(layer, 'dataset', requestGeneration.current)
    },
    [features, layers, loadFeaturePage],
  )

  const loadMoreDatasetFeatures = useCallback(
    async (layerId: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      const collection = features[layerId]?.dataset
      if (!layer || !collection || features[layerId]?.loadingDataset) return
      const wgs84 = features[layerId]?.wgs84
      const datasetOffset = collection.next_offset ?? collection.features.length
      const wgs84Offset = wgs84?.next_offset ?? wgs84?.features.length ?? 0
      await Promise.all([
        ...(datasetOffset < collection.total
          ? [
              loadFeaturePage(
                layer,
                'dataset',
                requestGeneration.current,
                datasetOffset,
                true,
              ),
            ]
          : []),
        ...(wgs84 && wgs84Offset < wgs84.total
          ? [
              loadFeaturePage(
                layer,
                'wgs84',
                requestGeneration.current,
                wgs84Offset,
                true,
              ),
            ]
          : []),
      ])
    },
    [features, layers, loadFeaturePage],
  )

  const upload = useCallback(
    async (files: File[], name?: string, crs?: string, encoding: OverlayEncoding = 'auto') => {
      if (!datasetId || !files.length) return
      if (demoMode) {
        notify?.({ tone: 'info', title: '데모 모드에서는 SHP를 등록할 수 없습니다.' })
        return
      }
      setUploading(true)
      try {
        const response = await api.uploadOverlay(datasetId, files, name, crs, encoding)
        notify?.({
          tone: 'success',
          title: 'SHP 레이어를 등록했습니다',
          message: `${response.layer.name} · ${response.layer.feature_count.toLocaleString('ko-KR')}개 피처`,
        })
        await refresh()
        setActiveLayerId(response.layer.id)
        clearSelectedDetails()
        setSelected(null)
      } catch (reason) {
        notify?.({
          tone: 'error',
          title: 'SHP 레이어를 등록하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      } finally {
        setUploading(false)
      }
    },
    [clearSelectedDetails, datasetId, demoMode, notify, refresh],
  )

  const removeLayer = useCallback(
    async (layerId: string) => {
      if (!datasetId || demoMode) return
      await api.deleteOverlay(datasetId, layerId)
      if (selected?.layerId === layerId) {
        clearSelectedDetails()
        setSelected(null)
      }
      if (activeLayerId === layerId) setActiveLayerId('')
      setPickTarget((current) => (current?.layerId === layerId ? null : current))
      const proposal = poleBaseProposalRef.current
      if (proposal.status !== 'idle' && proposal.target.layerId === layerId) {
        clearPoleBaseWorkflow()
      }
      notify?.({ tone: 'info', title: 'SHP 레이어 등록을 제거했습니다', message: '업로드 원본은 보존됩니다.' })
      await refresh()
    },
    [
      activeLayerId,
      clearPoleBaseWorkflow,
      clearSelectedDetails,
      datasetId,
      demoMode,
      notify,
      refresh,
      selected?.layerId,
    ],
  )

  const updateLayerMetadata = useCallback(
    async (layerId: string, patch: { name?: string; color?: string }) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!datasetId || !layer || demoMode) return
      try {
        const response = await api.patchOverlay(datasetId, layerId, {
          ...patch,
          expected_metadata_revision: layer.metadata_revision ?? 1,
        })
        setLayers((current) =>
          current.map((candidate) =>
            candidate.id === response.layer.id ? response.layer : candidate,
          ),
        )
        notify?.({ tone: 'success', title: 'SHP 레이어 표시 설정을 저장했습니다.' })
      } catch (reason) {
        await refresh()
        notify?.({
          tone: 'error',
          title: 'SHP 레이어 표시 설정을 저장하지 못했습니다.',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      }
    },
    [datasetId, demoMode, layers, notify, refresh],
  )

  const toggleLayer = useCallback(
    (layerId: string) => {
      const becomingVisible = !visibleLayerIds.has(layerId)
      setVisibleLayerIds((current) => {
        const next = new Set(current)
        if (next.has(layerId)) next.delete(layerId)
        else next.add(layerId)
        return next
      })
      if (becomingVisible) {
        const layer = layers.find((candidate) => candidate.id === layerId)
        if (layer && !features[layerId]?.wgs84 && !features[layerId]?.loadingWgs84) {
          void loadFeaturePage(layer, 'wgs84', requestGeneration.current)
        }
      }
    },
    [features, layers, loadFeaturePage, visibleLayerIds],
  )

  const selectFeature = useCallback(
    (selection: OverlaySelection | null, options?: OverlaySelectionOptions) => {
      clearSelectedDetails()
      setSelected(selection)
      if (!selection) return
      setActiveLayerId(selection.layerId)
      if (!visibleLayerIdsRef.current.has(selection.layerId)) {
        setVisibleLayerIds((current) => new Set(current).add(selection.layerId))
        const layer = layers.find((candidate) => candidate.id === selection.layerId)
        if (layer && !features[selection.layerId]?.wgs84 && !features[selection.layerId]?.loadingWgs84) {
          void loadFeaturePage(layer, 'wgs84', requestGeneration.current)
        }
      }
      if (options?.navigate !== false) {
        window.dispatchEvent(
          new CustomEvent('mms-overlay-selected', { detail: { datasetId, selection } }),
        )
      }
    },
    [clearSelectedDetails, datasetId, features, layers, loadFeaturePage],
  )

  const selectedLayer = useMemo(
    () => layers.find((layer) => layer.id === selected?.layerId) ?? null,
    [layers, selected?.layerId],
  )
  const selectedWgs84PageFeature = useMemo(
    () =>
      selected
        ? features[selected.layerId]?.wgs84?.features.find(
            (feature) => featureId(feature) === String(selected.featureId),
          ) ?? null
        : null,
    [features, selected],
  )
  const selectedDatasetPageFeature = useMemo(
    () =>
      selected
        ? features[selected.layerId]?.dataset?.features.find(
            (feature) => featureId(feature) === String(selected.featureId),
          ) ?? null
        : null,
    [features, selected],
  )
  const selectedWgs84DetailFeature = useMemo(
    () =>
      detailFeatureForSelection(
        selectedWgs84Detail,
        datasetId,
        selected,
        selectedLayer?.revision,
      ),
    [datasetId, selected, selectedLayer?.revision, selectedWgs84Detail],
  )
  const selectedDatasetDetailFeature = useMemo(
    () =>
      detailFeatureForSelection(
        selectedDatasetDetail,
        datasetId,
        selected,
        selectedLayer?.revision,
      ),
    [datasetId, selected, selectedDatasetDetail, selectedLayer?.revision],
  )

  useEffect(() => {
    if (!selected || !selectedLayer || !datasetId || demoMode) {
      selectionControllerRef.current?.abort()
      selectionControllerRef.current = null
      return
    }
    const coordinateSpaces: OverlayCoordinateSpace[] = []
    if (!selectedDatasetPageFeature && !selectedDatasetDetailFeature) {
      coordinateSpaces.push('dataset')
    }
    if (!selectedWgs84PageFeature && !selectedWgs84DetailFeature) {
      coordinateSpaces.push('wgs84')
    }
    if (coordinateSpaces.length === 0) return

    const requestId = selectionRequestRef.current + 1
    selectionRequestRef.current = requestId
    selectionControllerRef.current?.abort()
    const controller = new AbortController()
    selectionControllerRef.current = controller
    void Promise.allSettled(
      coordinateSpaces.map(async (coordinateSpace) => ({
        coordinateSpace,
        response: await api.overlayFeature(
          datasetId,
          selected.layerId,
          selected.featureId,
          coordinateSpace,
          controller.signal,
        ),
      })),
    ).then((results) => {
      if (selectionRequestRef.current !== requestId || controller.signal.aborted) return
      const failures: unknown[] = []
      results.forEach((result) => {
        if (result.status === 'rejected') {
          failures.push(result.reason)
          return
        }
        const { coordinateSpace, response } = result.value
        if (response.revision < selectedLayer.revision) return
        const detail: SelectedFeatureDetailCache = {
          datasetId,
          layerId: selected.layerId,
          featureId: selected.featureId,
          revision: response.revision,
          feature: response.feature,
        }
        if (coordinateSpace === 'wgs84') setSelectedWgs84Detail(detail)
        else setSelectedDatasetDetail(detail)
      })
      if (failures.length > 0) {
        const reason = failures[0]
        notify?.({
          tone: 'error',
          title:
            failures.length === coordinateSpaces.length
              ? '선택한 SHP 피처를 불러오지 못했습니다'
              : '선택한 SHP 피처의 일부 좌표를 불러오지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
      }
    })
    return () => {
      controller.abort()
      if (selectionControllerRef.current === controller) selectionControllerRef.current = null
    }
  }, [
    datasetId,
    demoMode,
    notify,
    selected,
    selectedDatasetDetailFeature,
    selectedDatasetPageFeature,
    selectedLayer,
    selectedWgs84DetailFeature,
    selectedWgs84PageFeature,
  ])
  const selectedFeature = useMemo(
    () =>
      selected
        ? selectedWgs84PageFeature ?? selectedWgs84DetailFeature
        : null,
    [selected, selectedWgs84DetailFeature, selectedWgs84PageFeature],
  )
  const selectedDatasetFeature = useMemo(
    () =>
      selected
        ? selectedDatasetPageFeature ?? selectedDatasetDetailFeature
        : null,
    [selected, selectedDatasetDetailFeature, selectedDatasetPageFeature],
  )

  const createFeature = useCallback(
    async (
      layerId: string,
      payload: Omit<OverlayFeatureCreateRequest, 'expected_revision'>,
      selectionOptions?: OverlaySelectionOptions,
    ) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!datasetId || !layer || demoMode) return
      const currentRevision =
        features[layerId]?.dataset?.revision ??
        features[layerId]?.wgs84?.revision ??
        layer.revision
      setCreatingFeature(true)
      try {
        const response = await api.createOverlayFeature(datasetId, layerId, {
          ...payload,
          expected_revision: currentRevision,
        })
        featureTaskResolutionPendingRef.current = response.task_resolution_pending ?? false
        savedFeatureIdRef.current = response.feature.id
        const nextSelection = { layerId, featureId: response.feature.id }
        selectFeature(nextSelection, selectionOptions)
        const detail: SelectedFeatureDetailCache = {
          datasetId,
          ...nextSelection,
          revision: response.revision,
          feature: response.feature,
        }
        if (response.coordinate_space === 'dataset') setSelectedDatasetDetail(detail)
        else setSelectedWgs84Detail(detail)
        const generation = requestGeneration.current
        const reloadDataset = Boolean(features[layerId]?.dataset)
        await Promise.all([
          loadFeaturePage(layer, 'wgs84', generation),
          ...(reloadDataset ? [loadFeaturePage(layer, 'dataset', generation)] : []),
        ])
        notify?.({ tone: 'success', title: '신규 SHP 피처를 추가했습니다' })
      } catch (reason) {
        notify?.({
          tone: 'error',
          title: '신규 SHP 피처를 추가하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      } finally {
        setCreatingFeature(false)
      }
    },
    [datasetId, demoMode, features, layers, loadFeaturePage, notify, selectFeature],
  )

  const updateSelected = useCallback(
    async (patch: {
      geometry?: { type: 'Point'; coordinates: [number, number, number?] }
      coordinate_space?: OverlayCoordinateSpace
      properties?: Record<string, unknown>
      review_metadata?: OverlayReviewMetadata
      manual_object_validation?: OverlayManualObjectValidation
      idempotency_key?: string
    }) => {
      if (!datasetId || !selected || !selectedLayer || demoMode) return
      const currentRevision =
        features[selected.layerId]?.dataset?.revision ?? selectedLayer.revision
      try {
        const response = await api.patchOverlayFeature(datasetId, selected.layerId, selected.featureId, {
          ...patch,
          expected_revision: currentRevision,
        })
        featureTaskResolutionPendingRef.current = response.task_resolution_pending ?? false
        savedFeatureIdRef.current = response.feature.id
        const detail: SelectedFeatureDetailCache = {
          datasetId,
          ...selected,
          revision: response.revision,
          feature: response.feature,
        }
        clearSelectedDetails()
        if (response.coordinate_space === 'dataset') setSelectedDatasetDetail(detail)
        else setSelectedWgs84Detail(detail)
        const generation = requestGeneration.current
        const reloadDataset = Boolean(features[selected.layerId]?.dataset)
        await Promise.all([
          loadFeaturePage(selectedLayer, 'wgs84', generation),
          ...(reloadDataset ? [loadFeaturePage(selectedLayer, 'dataset', generation)] : []),
        ])
        notify?.({ tone: 'success', title: 'SHP 피처를 저장했습니다' })
      } catch (reason) {
        notify?.({
          tone: 'error',
          title: 'SHP 피처를 저장하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      }
    },
    [clearSelectedDetails, datasetId, demoMode, features, loadFeaturePage, notify, selected, selectedLayer],
  )

  const applyPoleSeed = useCallback(
    async (frameId: string, coordinates: [number, number, number]) => {
      const proposal = poleBaseProposalRef.current
      if (proposal.status === 'idle' || !datasetId || !frameId) return
      const proposalFrameId = 'frameId' in proposal ? proposal.frameId : undefined
      if (proposal.status !== 'picking' && proposalFrameId === frameId) return
      if (coordinates.length !== 3 || coordinates.some((coordinate) => !Number.isFinite(coordinate))) {
        const message = poleBaseReasonMessage('INVALID_SEED')
        commitPoleBaseProposal({
          status: 'error',
          target: proposal.target,
          frameId,
          seed: coordinates,
          message,
          reasonCodes: ['INVALID_SEED'],
        })
        notify?.({ tone: 'error', title: '지주 하단을 산출하지 못했습니다', message })
        return
      }

      abortPoleBaseRequest()
      const requestId = poleBaseRequestIdRef.current + 1
      poleBaseRequestIdRef.current = requestId
      const controller = new AbortController()
      poleBaseRequestRef.current = { requestId, controller }
      const requestDatasetId = datasetId
      const target = proposal.target
      commitPoleBaseProposal({ status: 'loading', target, frameId, seed: coordinates })
      try {
        const result = await api.inferPoleBase(
          requestDatasetId,
          frameId,
          {
            coordinate_space: 'dataset',
            seed_position: coordinates,
            profile: 'balanced',
            debug: false,
          },
          controller.signal,
        )
        if (
          controller.signal.aborted ||
          poleBaseRequestIdRef.current !== requestId ||
          activeDatasetIdRef.current !== requestDatasetId
        ) {
          return
        }
        let templateValidation: PoleBaseTemplateValidation | undefined
        const workflow = stagedPointWorkflowRef.current
        const templateOptions =
          workflow && stagedWorkflowMatchesPoleTarget(workflow, target)
            ? workflow.templateOptions
            : undefined
        if (
          templateOptions &&
          result.status !== 'failed' &&
          result.base_position
        ) {
          const duplicate = await api.duplicateManualObjectPreflight(
            requestDatasetId,
            {
              target_layer_id: target.layerId,
              template_id: templateOptions.templateId,
              position: result.base_position,
              ...(target.kind === 'pole-base-move'
                ? { exclude_feature_id: String(target.featureId) }
                : {}),
            },
            controller.signal,
          )
          if (
            controller.signal.aborted ||
            poleBaseRequestIdRef.current !== requestId ||
            activeDatasetIdRef.current !== requestDatasetId
          ) {
            return
          }
          const latestWorkflow = stagedPointWorkflowRef.current
          const latestTemplateOptions =
            latestWorkflow && stagedWorkflowMatchesPoleTarget(latestWorkflow, target)
              ? (latestWorkflow.templateOptions ?? templateOptions)
              : templateOptions
          const layer = layers.find((candidate) => candidate.id === target.layerId)
          const fields =
            layer?.fields ??
            features[target.layerId]?.dataset?.fields ??
            features[target.layerId]?.wgs84?.fields ??
            []
          const currentProperties =
            target.kind === 'pole-base-move'
              ? (selectedDatasetFeature?.properties ?? selectedFeature?.properties ?? {})
              : {}
          const propertyResult = buildPoleBaseTemplatePropertyPatch(
            fields,
            currentProperties,
            result,
            frameId,
            latestTemplateOptions,
          )
          templateValidation = {
            duplicate,
            missingRequiredFields: propertyResult.missingRequiredFields,
            allowNearDuplicate: latestTemplateOptions.allowNearDuplicate,
            overrideReason: latestTemplateOptions.overrideReason,
          }
        }
        commitPoleBaseProposal({
          status: 'ready',
          target,
          frameId,
          seed: coordinates,
          result,
          idempotencyKey: featureMutationKey('pole-base'),
          ...(templateValidation ? { templateValidation } : {}),
        })
        const reasonMessage = poleBaseReasonMessages(result.reason_codes).join(' · ')
        if (result.status === 'failed') {
          notify?.({
            tone: 'info',
            title: '지주 하단을 산출하지 못했습니다',
            message: reasonMessage || '다른 지주점을 선택해 다시 시도해 주세요.',
          })
        } else if (result.status === 'review') {
          notify?.({
            tone: 'info',
            title: '지주 하단 산출 결과를 확인해 주세요',
            message: reasonMessage || result.warnings.join(' · ') || undefined,
          })
        }
      } catch (reason) {
        if (
          controller.signal.aborted ||
          isAbortError(reason) ||
          poleBaseRequestIdRef.current !== requestId ||
          activeDatasetIdRef.current !== requestDatasetId
        ) {
          return
        }
        const reasonCodes = poleBaseReasonCodesFromUnknown(reason)
        const mappedMessage = poleBaseReasonMessages(reasonCodes).join(' · ')
        const message = mappedMessage || (reason instanceof Error ? reason.message : '지주 하단 추론 요청에 실패했습니다.')
        commitPoleBaseProposal({
          status: 'error',
          target,
          frameId,
          seed: coordinates,
          message,
          reasonCodes,
        })
        notify?.({ tone: 'error', title: '지주 하단을 산출하지 못했습니다', message })
      } finally {
        if (poleBaseRequestRef.current?.requestId === requestId) {
          poleBaseRequestRef.current = null
        }
      }
    },
    [
      abortPoleBaseRequest,
      commitPoleBaseProposal,
      datasetId,
      features,
      layers,
      notify,
      selectedDatasetFeature,
      selectedFeature,
    ],
  )

  const confirmPoleBaseProposal = useCallback(async () => {
    const proposal = poleBaseProposalRef.current
    if (
      proposal.status !== 'ready' ||
      proposal.result.status === 'failed' ||
      !proposal.result.base_position ||
      poleBaseConfirmingRef.current
    ) {
      return false
    }
    const layer = layers.find((candidate) => candidate.id === proposal.target.layerId)
    if (!layer || !datasetId || demoMode) return false
    const target = proposal.target
    const stagedWorkflow = stagedPointWorkflowRef.current
    const matchingStagedWorkflow =
      stagedWorkflow && stagedWorkflowMatchesPoleTarget(stagedWorkflow, target)
        ? stagedWorkflow
        : null
    if (
      target.kind === 'pole-base-move' &&
      (!selected ||
        selected.layerId !== target.layerId ||
        String(selected.featureId) !== String(target.featureId))
    ) {
      notify?.({ tone: 'info', title: '재산출을 시작한 SHP 피처를 다시 선택해 주세요.' })
      return false
    }

    const fields =
      layer.fields ??
      features[target.layerId]?.dataset?.fields ??
      features[target.layerId]?.wgs84?.fields ??
      []
    const currentProperties =
      target.kind === 'pole-base-move'
        ? (selectedDatasetFeature?.properties ?? selectedFeature?.properties ?? {})
        : {}
    const templateOptions = matchingStagedWorkflow?.templateOptions
    const propertyResult = buildPoleBaseTemplatePropertyPatch(
      fields,
      currentProperties,
      proposal.result,
      proposal.frameId,
      templateOptions,
    )
    const properties = propertyResult.properties
    if (templateOptions) {
      const duplicate = proposal.templateValidation?.duplicate
      if (!duplicate) {
        notify?.({ tone: 'info', title: '중복 검사가 끝난 뒤 저장해 주세요.' })
        return false
      }
      if (propertyResult.missingRequiredFields.length > 0) {
        notify?.({
          tone: 'info',
          title: '필수 속성을 입력해 주세요.',
          message: propertyResult.missingRequiredFields.join(', '),
        })
        return false
      }
      if (duplicate.exact_duplicate) {
        notify?.({ tone: 'error', title: poleBaseReasonMessage('DUPLICATE_EXACT') })
        return false
      }
      if (
        duplicate.warning_count > 0 &&
        (!templateOptions.allowNearDuplicate || templateOptions.overrideReason.trim().length < 3)
      ) {
        notify?.({ tone: 'info', title: '근접 중복 저장 사유를 3자 이상 입력해 주세요.' })
        return false
      }
    }
    const propertyPayload = Object.keys(properties).length > 0 ? { properties } : {}
    const manualObjectValidation: OverlayManualObjectValidation | undefined = templateOptions
      ? {
          template_id: templateOptions.templateId,
          allow_near_duplicate: templateOptions.allowNearDuplicate,
          ...(templateOptions.allowNearDuplicate
            ? { override_reason: templateOptions.overrideReason.trim() }
            : {}),
        }
      : undefined
    const reviewMetadata: OverlayReviewMetadata = {
      source_frame_ids: [proposal.frameId],
      source_detection_ids: [],
      manual_observation_ids: [],
      creation_tool: 'manual_pole_base_v1',
      proposal_quality: proposal.result.quality.score,
      created_by: 'operator-local',
      ...(review?.currentTask?.dataset_id === datasetId &&
      review.currentTask.status === 'in_progress' &&
      (review.currentTask.target_layer_id === null ||
        review.currentTask.target_layer_id === target.layerId)
        ? { task_id: review.currentTask.id }
        : {}),
    }
    const saveId = poleBaseSaveIdRef.current + 1
    poleBaseSaveIdRef.current = saveId
    poleBaseConfirmingRef.current = true
    featureTaskResolutionPendingRef.current = null
    savedFeatureIdRef.current = null
    try {
      if (target.kind === 'pole-base-create') {
        await createFeature(target.layerId, {
          geometry: { type: 'Point', coordinates: proposal.result.base_position },
          coordinate_space: 'dataset',
          idempotency_key: proposal.idempotencyKey,
          ...propertyPayload,
          review_metadata: reviewMetadata,
          ...(manualObjectValidation
            ? { manual_object_validation: manualObjectValidation }
            : {}),
        }, { navigate: false })
      } else {
        await updateSelected({
          geometry: { type: 'Point', coordinates: proposal.result.base_position },
          coordinate_space: 'dataset',
          idempotency_key: proposal.idempotencyKey,
          ...propertyPayload,
          review_metadata: reviewMetadata,
          ...(manualObjectValidation
            ? { manual_object_validation: manualObjectValidation }
            : {}),
        })
      }
      if (
        featureTaskResolutionPendingRef.current &&
        reviewMetadata.task_id &&
        savedFeatureIdRef.current !== null
      ) {
        try {
          const resolution = target.kind === 'pole-base-create' ? 'manual_added' : 'corrected'
          const savedFeatureId = String(savedFeatureIdRef.current)
          const reconciled = await api.resolveReviewTask(reviewMetadata.task_id, {
            resolution,
            resolved_feature_ids: [String(savedFeatureIdRef.current)],
          })
          if (
            reconciled.task.status === resolution &&
            reconciled.task.resolved_feature_ids.some(
              (featureId) => String(featureId) === savedFeatureId,
            )
          ) {
            featureTaskResolutionPendingRef.current = false
          }
        } catch {
          // The feature transaction already committed. Keep the blocking
          // reconciliation state below instead of allowing a duplicate save.
        }
      }
      review?.reload()
      if (
        poleBaseSaveIdRef.current !== saveId ||
        activeDatasetIdRef.current !== datasetId ||
        poleBaseProposalRef.current !== proposal
      ) {
        return false
      }
      if (featureTaskResolutionPendingRef.current) {
        stagedPointWorkflowRef.current = null
        stagedPointCloudCoordinateRef.current = null
        directActualPointPickRef.current = false
        setPickTarget(null)
        const message = poleBaseReasonMessage('TASK_RESOLUTION_PENDING')
        commitPoleBaseProposal({
          status: 'error',
          target,
          frameId: proposal.frameId,
          seed: proposal.seed,
          message,
          reasonCodes: ['TASK_RESOLUTION_PENDING'],
        })
        notify?.({
          tone: 'error',
          title: message,
          message: '현재 task를 유지하고 목록을 새로고침했습니다. 동기화 상태를 확인해 주세요.',
        })
        return false
      }
      if (
        matchingStagedWorkflow?.target.kind === 'create' &&
        matchingStagedWorkflow.continuous
      ) {
        restoreStagedPointWorkflow(matchingStagedWorkflow)
      } else if (
        !matchingStagedWorkflow &&
        target.kind === 'pole-base-create' &&
        target.continuous
      ) {
        stagedPointWorkflowRef.current = null
        stagedPointCloudCoordinateRef.current = null
        directActualPointPickRef.current = false
        setPickTarget(target)
        commitPoleBaseProposal({ status: 'picking', target })
      } else {
        stagedPointWorkflowRef.current = null
        stagedPointCloudCoordinateRef.current = null
        directActualPointPickRef.current = false
        setPickTarget((current) => (current === target ? null : current))
        commitPoleBaseProposal({ status: 'idle' })
      }
      return true
    } catch (reason) {
      // The existing create/update helpers show the actionable server error.
      // Keep the ready proposal so a revision conflict can be refreshed and retried.
      if (reason instanceof ApiError && reason.status === 409) {
        await refresh()
        if (
          (reason.code === 'DUPLICATE_EXACT' || reason.code === 'DUPLICATE_NEARBY') &&
          proposal.templateValidation &&
          poleBaseProposalRef.current === proposal
        ) {
          const exact = reason.code === 'DUPLICATE_EXACT'
          commitPoleBaseProposal({
            ...proposal,
            templateValidation: {
              ...proposal.templateValidation,
              duplicate: {
                ...proposal.templateValidation.duplicate,
                exact_duplicate: exact,
                blocked: exact,
                warning_count: exact
                  ? proposal.templateValidation.duplicate.warning_count
                  : Math.max(1, proposal.templateValidation.duplicate.warning_count),
              },
            },
          })
          notify?.({
            tone: exact ? 'error' : 'info',
            title: poleBaseReasonMessage(reason.code),
            message: exact
              ? '다른 위치를 선택해 주세요.'
              : '방금 추가된 근접 객체를 확인하고 저장 사유를 입력해 주세요.',
          })
          return false
        }
        if (poleBaseProposalRef.current === proposal) {
          notify?.({
            tone: 'info',
            title: 'SHP 레이어의 최신 변경 내용을 불러왔습니다',
            message: '검토 중인 지주 하단 결과를 다시 저장해 주세요.',
          })
        }
      }
      return false
    } finally {
      if (poleBaseSaveIdRef.current === saveId) poleBaseConfirmingRef.current = false
    }
  }, [
    commitPoleBaseProposal,
    createFeature,
    datasetId,
    demoMode,
    features,
    layers,
    notify,
    refresh,
    review?.currentTask?.dataset_id,
    review?.currentTask?.id,
    review?.currentTask?.status,
    review?.currentTask?.target_layer_id,
    review?.reload,
    restoreStagedPointWorkflow,
    selected,
    selectedDatasetFeature,
    selectedFeature,
    updateSelected,
  ])

  const applyPickedCoordinate = useCallback(
    async (
      coordinates: [number, number, number?],
      coordinateSpace: OverlayCoordinateSpace,
    ) => {
      const target = pickTarget
      if (!target) return
      if (target.kind === 'pole-base-create' || target.kind === 'pole-base-move') return
      if (target.kind === 'create') {
        try {
          await createFeature(target.layerId, {
            geometry: { type: 'Point', coordinates },
            coordinate_space: coordinateSpace,
          })
          stagedPointWorkflowRef.current = null
          stagedPointCloudCoordinateRef.current = null
          directActualPointPickRef.current = false
          setPickTarget(null)
        } catch {
          // createFeature already emitted the actionable server error.
        }
        return
      }
      if (
        !selected ||
        selected.layerId !== target.layerId ||
        String(selected.featureId) !== String(target.featureId)
      ) {
        notify?.({ tone: 'info', title: '먼저 속성표에서 수정할 피처를 선택해 주세요.' })
        stagedPointWorkflowRef.current = null
        stagedPointCloudCoordinateRef.current = null
        directActualPointPickRef.current = false
        setPickTarget(null)
        return
      }
      try {
        await updateSelected({
          geometry: { type: 'Point', coordinates },
          coordinate_space: coordinateSpace,
        })
        stagedPointWorkflowRef.current = null
        stagedPointCloudCoordinateRef.current = null
        directActualPointPickRef.current = false
        setPickTarget(null)
      } catch {
        // updateSelected already emitted the actionable server error.
      }
    },
    [createFeature, notify, pickTarget, selected, updateSelected],
  )

  const applyPointCloudCoordinate = useCallback(
    async (frameId: string, coordinates: [number, number, number]) => {
      const target = pickTarget
      if (!target || target.kind === 'pole-base-create' || target.kind === 'pole-base-move') return
      if (directActualPointPickRef.current) {
        await applyPickedCoordinate(coordinates, 'dataset')
        return
      }
      if (!frameId || coordinates.some((coordinate) => !Number.isFinite(coordinate))) {
        notify?.({ tone: 'error', title: '선택한 Point 좌표가 올바르지 않습니다.' })
        return
      }
      if (
        target.kind === 'move' &&
        (!selected ||
          selected.layerId !== target.layerId ||
          String(selected.featureId) !== String(target.featureId))
      ) {
        notify?.({ tone: 'info', title: '먼저 속성표에서 수정할 피처를 선택해 주세요.' })
        return
      }
      const existingWorkflow = stagedPointWorkflowRef.current
      const workflow =
        existingWorkflow && directPointTargetsMatch(existingWorkflow.target, target)
          ? existingWorkflow
          : { target, continuous: false }
      if (poleBaseProposalRef.current.status !== 'idle') {
        clearPoleBaseWorkflow()
        setPickTarget(target)
      }
      stagedPointWorkflowRef.current = workflow
      stagedPointCloudCoordinateRef.current = { ...workflow, frameId, coordinates }
      directActualPointPickRef.current = false
      notify?.({
        tone: 'info',
        title: 'Point 좌표를 선택했습니다',
        message: 'B를 눌러 지주 하단을 산출하거나 P를 눌러 선택 좌표를 그대로 저장하세요.',
      })
    },
    [applyPickedCoordinate, clearPoleBaseWorkflow, notify, pickTarget, selected],
  )

  const startStagedPoleBaseInference = useCallback((): boolean => {
    const staged = stagedPointCloudCoordinateRef.current
    if (!staged) return false
    if (!poleBaseInferenceEnabled) {
      notify?.({ tone: 'info', title: '이 서버에서는 지주 하단 자동 산출을 사용할 수 없습니다.' })
      return true
    }
    const target: PoleBaseTarget =
      staged.target.kind === 'create'
        ? {
            kind: 'pole-base-create',
            layerId: staged.target.layerId,
            continuous: staged.continuous,
          }
        : {
            kind: 'pole-base-move',
            layerId: staged.target.layerId,
            featureId: staged.target.featureId,
          }
    abortPoleBaseRequest()
    poleBaseSaveIdRef.current += 1
    poleBaseConfirmingRef.current = false
    directActualPointPickRef.current = false
    setPickTarget(target)
    commitPoleBaseProposal({ status: 'picking', target })
    void applyPoleSeed(staged.frameId, staged.coordinates)
    return true
  }, [
    abortPoleBaseRequest,
    applyPoleSeed,
    commitPoleBaseProposal,
    notify,
    poleBaseInferenceEnabled,
  ])

  const confirmStagedActualPoint = useCallback(async () => {
    const staged = stagedPointCloudCoordinateRef.current
    if (!staged) return
    abortPoleBaseRequest()
    poleBaseSaveIdRef.current += 1
    poleBaseConfirmingRef.current = false
    commitPoleBaseProposal({ status: 'idle' })
    directActualPointPickRef.current = true
    setPickTarget(staged.target)
    try {
      if (staged.target.kind === 'create') {
        await createFeature(
          staged.target.layerId,
          {
            geometry: { type: 'Point', coordinates: staged.coordinates },
            coordinate_space: 'dataset',
          },
          staged.continuous ? { navigate: false } : undefined,
        )
      } else {
        if (
          !selected ||
          selected.layerId !== staged.target.layerId ||
          String(selected.featureId) !== String(staged.target.featureId)
        ) {
          notify?.({ tone: 'info', title: '먼저 속성표에서 수정할 피처를 선택해 주세요.' })
          return
        }
        await updateSelected({
          geometry: { type: 'Point', coordinates: staged.coordinates },
          coordinate_space: 'dataset',
        })
      }
      if (stagedPointCloudCoordinateRef.current === staged) {
        if (staged.target.kind === 'create' && staged.continuous) {
          restoreStagedPointWorkflow({ target: staged.target, continuous: true })
        } else {
          stagedPointWorkflowRef.current = null
          stagedPointCloudCoordinateRef.current = null
          directActualPointPickRef.current = false
          setPickTarget(null)
        }
      }
    } catch {
      // Existing create/update helpers already emitted the actionable error.
    }
  }, [
    abortPoleBaseRequest,
    commitPoleBaseProposal,
    createFeature,
    notify,
    restoreStagedPointWorkflow,
    selected,
    updateSelected,
  ])

  const shortcutStateRef = useRef({
    activeLayerId,
    beginCreatePoleBase,
    beginDirectActualPointPick,
    beginStagedPointCreate,
    cancelPoleBaseProposal,
    confirmStagedActualPoint,
    confirmPoleBaseProposal,
    pickMode,
    pickTarget,
    poleBaseProposal,
    retryPoleBasePick,
    selected,
    setPickMode,
    startStagedPoleBaseInference,
  })
  shortcutStateRef.current = {
    activeLayerId,
    beginCreatePoleBase,
    beginDirectActualPointPick,
    beginStagedPointCreate,
    cancelPoleBaseProposal,
    confirmStagedActualPoint,
    confirmPoleBaseProposal,
    pickMode,
    pickTarget,
    poleBaseProposal,
    retryPoleBasePick,
    selected,
    setPickMode,
    startStagedPoleBaseInference,
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        isTextEntryTarget(event.target)
      ) {
        return
      }
      const shortcut = shortcutStateRef.current
      if (event.key === 'Escape') {
        if (shortcut.poleBaseProposal.status !== 'idle') {
          event.preventDefault()
          if (shortcut.poleBaseProposal.status === 'picking') {
            shortcut.cancelPoleBaseProposal()
          } else {
            shortcut.retryPoleBasePick()
          }
        } else if (shortcut.pickMode) {
          event.preventDefault()
          shortcut.setPickMode(false)
        }
      } else if (event.code === 'KeyB') {
        event.preventDefault()
        if (event.repeat) return
        if (shortcut.poleBaseProposal.status === 'ready') {
          void shortcut.confirmPoleBaseProposal()
        } else if (shortcut.poleBaseProposal.status === 'idle') {
          if (!shortcut.startStagedPoleBaseInference()) {
            shortcut.beginCreatePoleBase(shortcut.activeLayerId)
          }
        }
      } else if (event.key === 'Enter' && shortcut.poleBaseProposal.status === 'ready') {
        event.preventDefault()
        if (event.repeat) return
        void shortcut.confirmPoleBaseProposal()
      } else if (event.code === 'KeyR' && shortcut.poleBaseProposal.status !== 'idle') {
        event.preventDefault()
        if (event.repeat) return
        shortcut.retryPoleBasePick()
      } else if (event.code === 'KeyN') {
        event.preventDefault()
        if (event.repeat) return
        if (
          shortcut.pickTarget?.kind === 'create' &&
          shortcut.pickTarget.layerId === shortcut.activeLayerId
        ) {
          shortcut.setPickMode(false)
        } else {
          shortcut.beginStagedPointCreate(shortcut.activeLayerId, false)
        }
      } else if (event.code === 'KeyP') {
        if (stagedPointCloudCoordinateRef.current) {
          event.preventDefault()
          void shortcut.confirmStagedActualPoint()
        } else if (shortcut.poleBaseProposal.status !== 'idle') {
          event.preventDefault()
          if (shortcut.poleBaseProposal.target.kind === 'pole-base-create') {
            shortcut.beginDirectActualPointPick({
              kind: 'create',
              layerId: shortcut.poleBaseProposal.target.layerId,
            })
          } else if (shortcut.selected) {
            shortcut.beginDirectActualPointPick({
              kind: 'move',
              layerId: shortcut.poleBaseProposal.target.layerId,
              featureId: shortcut.poleBaseProposal.target.featureId,
            })
          }
        } else if (shortcut.selected) {
          event.preventDefault()
          if (
            directActualPointPickRef.current &&
            shortcut.pickTarget?.kind === 'move' &&
            shortcut.pickTarget.layerId === shortcut.selected.layerId &&
            String(shortcut.pickTarget.featureId) === String(shortcut.selected.featureId)
          ) {
            shortcut.setPickMode(false)
          } else {
            shortcut.beginDirectActualPointPick({
              kind: 'move',
              layerId: shortcut.selected.layerId,
              featureId: shortcut.selected.featureId,
            })
          }
        }
      }
    }
    // Keep edit shortcuts global even when the focused map/viewer surface has
    // its own key handler. Point creation deliberately leaves focus there.
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [])

  const copySelectedLocation = useCallback(async () => {
    if (!selected || !selectedLayer) {
      notify?.({ tone: 'info', title: '위치를 복사할 SHP 피처를 먼저 선택해 주세요.' })
      return
    }
    await createFeature(selected.layerId, {
      copy_geometry_from: selected.featureId,
      coordinate_space: 'dataset',
    })
  }, [createFeature, notify, selected, selectedLayer])

  const deleteSelected = useCallback(async () => {
    if (!datasetId || !selected || !selectedLayer || demoMode) return
    const currentRevision = features[selected.layerId]?.dataset?.revision ?? selectedLayer.revision
    await api.deleteOverlayFeature(datasetId, selected.layerId, selected.featureId, currentRevision)
    clearSelectedDetails()
    setSelected(null)
    setPickTarget(null)
    const proposal = poleBaseProposalRef.current
    if (
      proposal.status !== 'idle' &&
      proposal.target.kind === 'pole-base-move' &&
      proposal.target.layerId === selected.layerId &&
      String(proposal.target.featureId) === String(selected.featureId)
    ) {
      clearPoleBaseWorkflow()
    }
    notify?.({ tone: 'info', title: '선택한 SHP 피처를 삭제했습니다' })
    const generation = requestGeneration.current
    const reloadDataset = Boolean(features[selected.layerId]?.dataset)
    await Promise.all([
      loadFeaturePage(selectedLayer, 'wgs84', generation),
      ...(reloadDataset ? [loadFeaturePage(selectedLayer, 'dataset', generation)] : []),
    ])
  }, [
    clearPoleBaseWorkflow,
    clearSelectedDetails,
    datasetId,
    demoMode,
    features,
    loadFeaturePage,
    notify,
    selected,
    selectedLayer,
  ])

  const deleteField = useCallback(
    async (layerId: string, fieldName: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!datasetId || !layer || demoMode) return
      const currentRevision =
        features[layerId]?.dataset?.revision ??
        features[layerId]?.wgs84?.revision ??
        layer.revision
      try {
        await api.deleteOverlayField(datasetId, layerId, fieldName, currentRevision)
        if (selected?.layerId === layerId) clearSelectedDetails()
        notify?.({
          tone: 'info',
          title: `SHP 속성 열 '${fieldName}'을 삭제했습니다`,
          message: '업로드 원본은 보존됩니다.',
        })
        await refresh()
      } catch (reason) {
        await refresh()
        notify?.({
          tone: 'error',
          title: 'SHP 속성 열을 삭제하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      }
    },
    [clearSelectedDetails, datasetId, demoMode, features, layers, notify, refresh, selected?.layerId],
  )

  const mapFeatures = useMemo(
    () =>
      layers.flatMap((layer) => {
        if (!visibleLayerIds.has(layer.id)) return []
        const color = layerColor(layer.id)
        const pageFeatures = features[layer.id]?.wgs84?.features ?? []
        const selectedForLayer = selected?.layerId === layer.id ? selectedFeature : null
        const layerFeatures =
          selectedForLayer &&
          !pageFeatures.some(
            (feature) => featureId(feature) === String(selectedForLayer.id),
          )
            ? [...pageFeatures, selectedForLayer]
            : pageFeatures
        return layerFeatures.map((feature) => ({
          ...feature,
          properties: {
            ...feature.properties,
            __overlay_layer_id: layer.id,
            __overlay_feature_id: String(feature.id),
            __overlay_color: color,
            __overlay_selected:
              selected?.layerId === layer.id && String(selected.featureId) === String(feature.id)
                ? 1
                : 0,
          },
        }))
      }),
    [features, layerColor, layers, selected, selectedFeature, visibleLayerIds],
  )

  const datasetFeatures = useMemo(
    () =>
      layers.flatMap((layer) =>
        visibleLayerIds.has(layer.id)
          ? (features[layer.id]?.dataset?.features ?? []).map((feature) => ({
              layerId: layer.id,
              color: layerColor(layer.id),
              feature,
            }))
          : [],
      ),
    [features, layerColor, layers, visibleLayerIds],
  )

  const value = useMemo<OverlayContextValue>(
    () => ({
      datasetId,
      layers,
      features,
      visibleLayerIds,
      activeLayerId,
      setActiveLayerId,
      selected,
      selectedLayer,
      selectedFeature,
      selectedDatasetFeature,
      mapFeatures,
      datasetFeatures,
      loading,
      uploading,
      creatingFeature,
      pickMode,
      pickTarget,
      poleBaseProposal,
      poleBaseInferenceEnabled,
      setPickMode,
      beginCreatePoint,
      beginStagedPointCreate,
      beginStagedSelectedPointMove,
      updateStagedPoleBaseTemplateOptions,
      beginCreatePoleBase,
      beginRecomputeSelectedPoleBase,
      applyPoleSeed,
      confirmPoleBaseProposal,
      retryPoleBasePick,
      cancelPoleBaseProposal,
      handlePoleBaseFrameChange,
      refresh,
      ensureDatasetFeatures,
      loadMoreDatasetFeatures,
      upload,
      updateLayerMetadata,
      removeLayer,
      toggleLayer,
      selectFeature,
      updateSelected,
      applyPickedCoordinate,
      applyPointCloudCoordinate,
      copySelectedLocation,
      deleteSelected,
      deleteField,
      layerColor,
    }),
    [
      applyPickedCoordinate,
      applyPointCloudCoordinate,
      applyPoleSeed,
      activeLayerId,
      beginCreatePoint,
      beginStagedPointCreate,
      beginStagedSelectedPointMove,
      updateStagedPoleBaseTemplateOptions,
      beginCreatePoleBase,
      beginRecomputeSelectedPoleBase,
      cancelPoleBaseProposal,
      confirmPoleBaseProposal,
      copySelectedLocation,
      creatingFeature,
      datasetFeatures,
      datasetId,
      deleteSelected,
      deleteField,
      features,
      ensureDatasetFeatures,
      handlePoleBaseFrameChange,
      layerColor,
      layers,
      loading,
      loadMoreDatasetFeatures,
      mapFeatures,
      pickMode,
      pickTarget,
      poleBaseProposal,
      poleBaseInferenceEnabled,
      refresh,
      removeLayer,
      selected,
      selectedDatasetFeature,
      selectedFeature,
      selectedLayer,
      selectFeature,
      setPickMode,
      retryPoleBasePick,
      toggleLayer,
      updateSelected,
      updateLayerMetadata,
      upload,
      uploading,
      visibleLayerIds,
    ],
  )

  return <OverlayContext.Provider value={value}>{children}</OverlayContext.Provider>
}

export function useOverlayWorkspace(): OverlayContextValue {
  const value = useContext(OverlayContext)
  if (!value) throw new Error('useOverlayWorkspace must be used inside OverlayProvider.')
  return value
}

export function useOptionalOverlayWorkspace(): OverlayContextValue | null {
  return useContext(OverlayContext)
}
