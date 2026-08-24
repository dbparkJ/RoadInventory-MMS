import {
  Activity,
  Bot,
  Check,
  ChevronDown,
  CircleHelp,
  Gauge,
  Play,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  WandSparkles,
} from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { api, ApiError } from '../lib/api'
import type {
  AutoPreset,
  DatasetSummary,
  DetectionModelOption,
  Frame,
  FrameRange,
  ManualParameters,
  ParameterMode,
  RunRequest,
} from '../types'
import { FrameRangePicker } from './FrameRangePicker'

const DEFAULT_PARAMETERS: ManualParameters = {
  voxel_size: 0.1,
  confidence: 0.8,
  cluster_distance: 0.35,
  min_points: 100,
  search_radius: 15,
  ground_tolerance: 0.35,
}

const PARAMETER_FIELDS: Array<{
  key: keyof ManualParameters
  label: string
  description: string
  unit: string
  min: number
  max: number
  step: number
}> = [
  {
    key: 'voxel_size',
    label: '복셀 크기',
    description: '포인트 다운샘플 간격',
    unit: 'm',
    min: 0.01,
    max: 0.5,
    step: 0.01,
  },
  {
    key: 'confidence',
    label: '최소 신뢰도',
    description: '검출 결과 채택 기준',
    unit: '',
    min: 0.05,
    max: 0.99,
    step: 0.01,
  },
  {
    key: 'cluster_distance',
    label: '군집 거리',
    description: '인접 포인트 결합 반경',
    unit: 'm',
    min: 0.05,
    max: 2,
    step: 0.01,
  },
  {
    key: 'min_points',
    label: '최소 포인트',
    description: '유효 군집의 최소 크기',
    unit: 'pts',
    min: 3,
    max: 500,
    step: 1,
  },
  {
    key: 'search_radius',
    label: '탐색 반경',
    description: '후보 주변 공간 범위',
    unit: 'm',
    min: 0.5,
    max: 60,
    step: 0.1,
  },
  {
    key: 'ground_tolerance',
    label: '지면 허용차',
    description: '지면 모델 오차 범위',
    unit: 'm',
    min: 0.01,
    max: 1,
    step: 0.01,
  },
]

const PRESETS: Array<{
  id: AutoPreset
  label: string
  description: string
  accent: string
  icon: typeof Gauge
}> = [
  { id: 'fast', label: '절약', description: '낮은 자원 사용', accent: '가장 가벼움', icon: Gauge },
  { id: 'balanced', label: '균형', description: '표준 자원 배분', accent: '권장', icon: Activity },
  { id: 'precise', label: '최대', description: '가용 자원 활용', accent: '높은 처리량', icon: Sparkles },
]

export function OptimizationPanel({
  dataset,
  selectedTrack,
  selectedFrame,
  frameRange,
  busy,
  externalAction,
  onStart,
  onOptimize,
  onSetFrameRangeStart,
  onSetFrameRangeEnd,
  onFrameRangeChange,
  onClearFrameRange,
}: {
  dataset: DatasetSummary | null
  selectedTrack: string
  selectedFrame: Frame | null
  frameRange: FrameRange | null
  busy: boolean
  externalAction?: ReactNode
  onStart: (request: RunRequest) => Promise<void>
  onOptimize: (request: RunRequest) => Promise<ManualParameters | undefined>
  onSetFrameRangeStart: (ordinal: number) => void
  onSetFrameRangeEnd: (ordinal: number) => void
  onFrameRangeChange: (range: FrameRange) => void
  onClearFrameRange: () => void
}) {
  const [mode, setMode] = useState<ParameterMode>('automatic')
  const [parameters, setParameters] = useState(DEFAULT_PARAMETERS)
  const [preset, setPreset] = useState<AutoPreset>('balanced')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [optimizing, setOptimizing] = useState(false)
  const [layerName, setLayerName] = useState('')
  const [layerNameDatasetId, setLayerNameDatasetId] = useState<string | null>(null)
  const [modelOptions, setModelOptions] = useState<DetectionModelOption[]>([])
  const [selectedModelNames, setSelectedModelNames] = useState<Set<string>>(new Set())
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelsError, setModelsError] = useState<string | null>(null)
  const [legacyModelApi, setLegacyModelApi] = useState(false)
  const [modelsDatasetId, setModelsDatasetId] = useState<string | null>(null)
  const [modelRequestVersion, setModelRequestVersion] = useState(0)

  useEffect(() => {
    setParameters(DEFAULT_PARAMETERS)
    setLayerName(dataset ? `${dataset.name} 검출레이어` : '')
    setLayerNameDatasetId(dataset?.id ?? null)
  }, [dataset?.id])

  const datasetReady = dataset?.status === 'ready'
  useEffect(() => {
    setModelOptions([])
    setSelectedModelNames(new Set())
    setModelsError(null)
    setLegacyModelApi(false)
    setModelsDatasetId(null)
    if (!datasetReady) {
      setModelsLoading(false)
      return
    }
    const controller = new AbortController()
    setModelsLoading(true)
    void api.detectionModels(controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return
        setModelOptions(response.items)
        const available = new Set(response.items.map((model) => model.id))
        const defaults = response.default_model_ids.filter((id) => available.has(id))
        setSelectedModelNames(new Set(defaults.length ? defaults : available))
        setLegacyModelApi(false)
        setModelsDatasetId(dataset?.id ?? null)
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          const legacyApi = reason instanceof ApiError && reason.status === 404
          setLegacyModelApi(legacyApi)
          setModelsError(legacyApi
            ? null
            : reason instanceof Error
              ? reason.message
              : '검출 모델 목록을 불러오지 못했습니다.')
          setModelsDatasetId(dataset?.id ?? null)
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setModelsLoading(false)
      })
    return () => controller.abort()
  }, [dataset?.id, datasetReady, modelRequestVersion])

  const selectedScopeFrameCount = selectedTrack
    ? dataset?.tracks.find((track) => track.id === selectedTrack)?.frame_count
    : dataset?.frame_count
  const normalizedLayerName = layerName.trim()
  const runIdentityValid = (
    layerNameDatasetId === dataset?.id && normalizedLayerName.length > 0
  )
  const modelSelectionValid = (
    modelsDatasetId === dataset?.id
    && !modelsLoading
    && (legacyModelApi
      || (modelsError === null && modelOptions.length > 0 && selectedModelNames.size > 0))
  )
  const request: RunRequest | null = dataset
    && datasetReady
    && runIdentityValid
    && modelSelectionValid
    ? {
        dataset_id: dataset.id,
        track_ids: selectedTrack
          ? [selectedTrack]
          : dataset.tracks.map((track) => track.id),
        frame_range: frameRange,
        mode,
        run_name: normalizedLayerName,
        layer_name: normalizedLayerName,
        ...(!legacyModelApi && modelOptions.length
          ? { model_names: modelOptions
              .filter((model) => selectedModelNames.has(model.id))
              .map((model) => model.name) }
          : {}),
        ...(mode === 'manual'
          ? { parameters }
          : { auto: { preset } }),
      }
    : null

  const toggleModel = (modelId: string) => {
    setSelectedModelNames((current) => {
      const next = new Set(current)
      if (next.has(modelId)) next.delete(modelId)
      else next.add(modelId)
      return next
    })
  }

  const autoTune = async () => {
    if (!request) return
    setOptimizing(true)
    try {
      await onOptimize({
        ...request,
        mode: 'automatic',
        auto: { preset },
      })
    } finally {
      setOptimizing(false)
    }
  }

  return (
    <aside className="inspector" aria-label="작업 설정">
      <div className="panel-heading inspector-heading">
        <div>
          <span className="eyebrow">PROCESS SETUP</span>
          <h2>작업 설정</h2>
        </div>
        <div className="panel-heading-actions">
          <span className="beta-tag">BETA</span>
          {externalAction}
        </div>
      </div>

      <div className="inspector-scroll">
        <FrameRangePicker
          frameLimit={Math.max(1, dataset?.frame_count ?? 1)}
          selectedFrame={selectedFrame}
          frameRange={frameRange}
          onSetStart={onSetFrameRangeStart}
          onSetEnd={onSetFrameRangeEnd}
          onChange={onFrameRangeChange}
          onClear={onClearFrameRange}
        />

        <section className="setup-section">
          <div className="section-label">
            <span>파라미터 모드</span>
            <CircleHelp size={14} />
          </div>
          <div className="mode-switch" role="radiogroup" aria-label="파라미터 입력 모드">
            <button
              type="button"
              role="radio"
              aria-checked={mode === 'automatic'}
              className={mode === 'automatic' ? 'active' : ''}
              onClick={() => setMode('automatic')}
            >
              <Bot size={17} />
              <span>
                <strong>자동 설정</strong>
                <small>작업 자원 자동 조정</small>
              </span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={mode === 'manual'}
              className={mode === 'manual' ? 'active' : ''}
              onClick={() => setMode('manual')}
            >
              <SlidersHorizontal size={17} />
              <span>
                <strong>수치 입력</strong>
                <small>직접 세부 조정</small>
              </span>
            </button>
          </div>
        </section>

        <section className="setup-section detection-run-identity">
          <div className="section-label">
            <span>검출 레이어와 모델</span>
            <small>모델 1개 이상</small>
          </div>
          <label className="detection-layer-name">
            <span>검출 레이어 이름</span>
            <input
              type="text"
              value={layerName}
              maxLength={120}
              aria-label="검출 레이어 이름"
              placeholder="예: 2026년 표지판 검출"
              onChange={(event) => setLayerName(event.target.value)}
            />
            <small>완료 작업의 기본 실행 이름과 결과 SHP 레이어 이름으로 사용합니다.</small>
          </label>
          {modelsLoading ? (
            <p className="detection-model-status" role="status">검출 모델을 확인하고 있습니다.</p>
          ) : modelsError ? (
            <div className="detection-model-status error" role="alert">
              <span>모델 목록을 불러오지 못했습니다. 모델을 확인한 뒤 다시 시도해 주세요.</span>
              <small>{modelsError}</small>
              <button
                type="button"
                className="text-action"
                onClick={() => setModelRequestVersion((version) => version + 1)}
              >
                다시 시도
              </button>
            </div>
          ) : legacyModelApi ? (
            <p className="detection-model-status" role="status">
              구버전 서버에서는 모델 목록을 제공하지 않아 기존 전체 모델 설정으로 실행합니다.
            </p>
          ) : (
            <div className="detection-model-picker">
              <div className="detection-model-actions">
                <strong>사용 모델</strong>
                <button
                  type="button"
                  className="text-action"
                  onClick={() => setSelectedModelNames(
                    selectedModelNames.size === modelOptions.length
                      ? new Set()
                      : new Set(modelOptions.map((model) => model.id)),
                  )}
                >
                  {selectedModelNames.size === modelOptions.length ? '전체 해제' : '전체 선택'}
                </button>
              </div>
              <div className="detection-model-list">
                {modelOptions.map((model) => (
                  <label key={model.id}>
                    <input
                      type="checkbox"
                      checked={selectedModelNames.has(model.id)}
                      onChange={() => toggleModel(model.id)}
                    />
                    <span><strong>{model.label}</strong><small>{model.name}</small></span>
                  </label>
                ))}
              </div>
              {!modelOptions.length && (
                <p className="detection-model-status error">사용 가능한 모델이 없습니다.</p>
              )}
              {modelOptions.length > 0 && selectedModelNames.size === 0 && (
                <p className="detection-model-status error" role="alert">
                  실행할 모델을 한 개 이상 선택해 주세요.
                </p>
              )}
            </div>
          )}
        </section>

        {mode === 'automatic' ? (
          <>
            <section className="setup-section">
              <div className="section-label">
                <span>처리 자원 프로필</span>
                <small>1개 선택</small>
              </div>
              <div className="preset-grid">
                {PRESETS.map((option) => {
                  const Icon = option.icon
                  return (
                    <button
                      type="button"
                      key={option.id}
                      className={preset === option.id ? 'active' : ''}
                      onClick={() => setPreset(option.id)}
                    >
                      <span className="preset-check">{preset === option.id && <Check size={11} />}</span>
                      <Icon size={18} />
                      <strong>{option.label}</strong>
                      <small>{option.description}</small>
                      <em>{option.accent}</em>
                    </button>
                  )
                })}
              </div>
            </section>

            <section className="setup-section">
              <div className="auto-callout">
                <WandSparkles size={18} />
                <div>
                  <strong>검증된 검출 기준은 그대로 유지합니다</strong>
                  <p>
                    서버가 장비 사양과 선택한 자원 프로필에 맞춰 병렬 처리량과 메모리 사용량을
                    조정합니다. 학습이나 임의의 임계값 변경은 수행하지 않습니다.
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="button ghost full"
                onClick={autoTune}
                disabled={!request || optimizing}
              >
                {optimizing ? <span className="button-spinner" /> : <Sparkles size={15} />}
                서버 자동 설정 확인
              </button>
            </section>
          </>
        ) : (
          <section className="setup-section parameter-section">
            <div className="section-label">
              <span>검출 파라미터</span>
              <button
                type="button"
                className="text-action"
                onClick={() => setParameters(DEFAULT_PARAMETERS)}
              >
                기본값 복원
              </button>
            </div>
            <div className="parameter-list">
              {PARAMETER_FIELDS.slice(0, showAdvanced ? undefined : 4).map((field) => (
                <label key={field.key} className="parameter-row">
                  <span>
                    <strong>{field.label}</strong>
                    <small>{field.description}</small>
                  </span>
                  <span className="number-input">
                    <input
                      type="number"
                      value={parameters[field.key]}
                      min={field.min}
                      max={field.max}
                      step={field.step}
                      onChange={(event) =>
                        setParameters((current) => ({
                          ...current,
                          [field.key]: Math.min(
                            field.max,
                            Math.max(field.min, Number(event.target.value)),
                          ),
                        }))
                      }
                    />
                    {field.unit && <em>{field.unit}</em>}
                  </span>
                </label>
              ))}
            </div>
            <button
              type="button"
              className="advanced-toggle"
              onClick={() => setShowAdvanced((value) => !value)}
            >
              <Settings2 size={14} />
              고급 파라미터 {showAdvanced ? '접기' : '펼치기'}
              <ChevronDown className={showAdvanced ? 'open' : ''} size={14} />
            </button>
          </section>
        )}
      </div>

      <footer className="inspector-footer">
        <div className="run-summary">
          <span>
            <i className={datasetReady ? 'ready' : dataset?.status === 'error' ? 'error' : ''} />
            {!dataset
              ? '데이터 미선택'
              : dataset.status === 'indexing'
                ? '데이터 준비 중'
                : dataset.status === 'error'
                  ? '데이터 오류'
                  : selectedTrack
                    ? '선택 구간'
                    : '전체 구간'}
          </span>
          <small>
            {dataset && selectedScopeFrameCount !== undefined
              ? `${selectedScopeFrameCount.toLocaleString('ko-KR')} frames`
              : '—'}
          </small>
        </div>
        <div className="run-range-summary" aria-live="polite">
          <span>실행 프레임</span>
          <strong>
            {frameRange
              ? `${String(frameRange[0] + 1).padStart(4, '0')}–${String(
                  frameRange[1] + 1,
                ).padStart(4, '0')}`
              : '구간 전체'}
          </strong>
          <small>{frameRange ? `ordinal ${frameRange[0]}–${frameRange[1]}` : 'ALL'}</small>
        </div>
        <button
          type="button"
          className="button primary full run-button"
          disabled={!request || busy}
          onClick={() => request && void onStart(request)}
        >
          {busy ? <span className="button-spinner dark" /> : <Play size={16} fill="currentColor" />}
          {busy ? '요청 전송 중' : '작업 시작'}
        </button>
        <p className="footer-note">작업은 서버 큐에서 실행되며 창을 닫아도 계속됩니다.</p>
      </footer>
    </aside>
  )
}

export { DEFAULT_PARAMETERS }
