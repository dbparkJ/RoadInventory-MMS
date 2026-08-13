import {
  Box,
  Camera,
  Check,
  ChevronDown,
  Cloud,
  Database,
  Flag,
  Gauge,
  Layers3,
  LocateFixed,
  LoaderCircle,
  PanelLeftClose,
  PanelLeftOpen,
  RotateCcw,
  Trash2,
} from 'lucide-react'
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { DatasetSummary, Frame, FrameRange } from '../types'
import { formatCount, formatDistance } from '../lib/format'
import { TRACK_COLORS } from '../lib/route'
import { DatasetOverlayBrowser } from './DatasetOverlayBrowser'

const DATASET_STATUS: Record<DatasetSummary['status'], string> = {
  ready: '인덱스 준비됨',
  indexing: '인덱싱 중',
  error: '인덱싱 오류',
}

interface DatasetPanelProps {
  datasets: DatasetSummary[]
  selectedDataset: DatasetSummary | null
  selectedTrack: string
  frames: Frame[]
  selectedFrame: Frame | null
  framesLoading: boolean
  framesLoadingMore: boolean
  frameTotal: number
  hasMoreFrames: boolean
  frameRange: FrameRange | null
  focusOverlayLayerId?: string
  removingDataset?: boolean
  externalAction?: ReactNode
  collapsed?: boolean
  onDatasetChange: (id: string) => void
  onTrackChange: (id: string) => void
  onFrameChange: (frame: Frame) => void
  onSetFrameRangeStart: (ordinal: number) => void
  onSetFrameRangeEnd: (ordinal: number) => void
  onFrameRangeChange: (range: FrameRange) => void
  onClearFrameRange: () => void
  onLoadMoreFrames: () => void
  onOpenSource: () => void
  onRemoveDataset?: (dataset: DatasetSummary) => void
  onToggleCollapsed?: () => void
}

export function DatasetPanel({
  datasets,
  selectedDataset,
  selectedTrack,
  selectedFrame,
  frameRange,
  focusOverlayLayerId,
  removingDataset = false,
  externalAction,
  collapsed = false,
  onDatasetChange,
  onTrackChange,
  onSetFrameRangeStart,
  onSetFrameRangeEnd,
  onFrameRangeChange,
  onClearFrameRange,
  onOpenSource,
  onRemoveDataset,
  onToggleCollapsed,
}: DatasetPanelProps) {
  const [rangeDraft, setRangeDraft] = useState<[string, string]>(['', ''])
  const [datasetMenuOpen, setDatasetMenuOpen] = useState(false)
  const datasetPickerRef = useRef<HTMLDivElement>(null)
  const datasetTriggerRef = useRef<HTMLButtonElement>(null)
  const datasetListId = useId()
  const frameLimit = Math.max(1, selectedDataset?.frame_count ?? 1)
  const parsedRange = rangeDraft.map((value) => Number(value)) as [number, number]
  const rangeDraftValid = parsedRange.every(
    (value) => Number.isInteger(value) && value >= 1 && value <= frameLimit,
  )

  useEffect(() => {
    setRangeDraft(
      frameRange ? [String(frameRange[0] + 1), String(frameRange[1] + 1)] : ['', ''],
    )
  }, [frameRange, selectedDataset?.id])

  useEffect(() => {
    if (!datasetMenuOpen) return
    const ownerDocument = datasetPickerRef.current?.ownerDocument ?? document
    const closeForOutsidePointer = (event: PointerEvent) => {
      if (!datasetPickerRef.current?.contains(event.target as Node)) setDatasetMenuOpen(false)
    }
    const closeForEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setDatasetMenuOpen(false)
      datasetTriggerRef.current?.focus()
    }
    ownerDocument.addEventListener('pointerdown', closeForOutsidePointer)
    ownerDocument.addEventListener('keydown', closeForEscape)
    return () => {
      ownerDocument.removeEventListener('pointerdown', closeForOutsidePointer)
      ownerDocument.removeEventListener('keydown', closeForEscape)
    }
  }, [datasetMenuOpen])

  useEffect(() => setDatasetMenuOpen(false), [selectedDataset?.id])

  const applyRangeDraft = () => {
    if (!rangeDraftValid) return
    const start = Math.min(parsedRange[0], parsedRange[1]) - 1
    const end = Math.max(parsedRange[0], parsedRange[1]) - 1
    onFrameRangeChange([start, end])
  }

  const focusDatasetOption = (position: 'selected' | 'first' | 'last') => {
    window.requestAnimationFrame(() => {
      const options = Array.from(
        datasetPickerRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [],
      )
      if (!options.length) return
      const selectedIndex = Math.max(
        0,
        datasets.findIndex((dataset) => dataset.id === selectedDataset?.id),
      )
      const index = position === 'first' ? 0 : position === 'last' ? options.length - 1 : selectedIndex
      options[index]?.focus()
    })
  }

  const openDatasetMenu = () => {
    setDatasetMenuOpen(true)
    focusDatasetOption('selected')
  }

  const handleDatasetListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const options = Array.from(
      datasetPickerRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [],
    )
    if (!options.length) return
    const currentIndex = Math.max(0, options.indexOf(event.target as HTMLButtonElement))
    let nextIndex: number | null = null
    if (event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % options.length
    if (event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + options.length) % options.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = options.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    options[nextIndex]?.focus()
  }

  return (
    <aside
      className={`data-panel ${collapsed ? 'collapsed' : ''}`}
      aria-label="데이터 탐색기"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      {collapsed && onToggleCollapsed ? (
        <button
          type="button"
          className="data-panel-restore-hitarea"
          onClick={onToggleCollapsed}
          title="작업 데이터 목록 열기"
          aria-label="작업 데이터 패널 복원"
          aria-expanded="false"
        >
          <PanelLeftOpen size={18} aria-hidden="true" />
          <span>DATA</span>
        </button>
      ) : (
        <div className="panel-heading">
          <div className="panel-heading-copy">
            <span className="eyebrow">DATA EXPLORER</span>
            <h2>작업 데이터</h2>
          </div>
          <div className="panel-heading-actions">
            {externalAction}
            <button type="button" className="icon-button" onClick={onOpenSource} title="데이터 추가">
              <Cloud size={17} />
            </button>
            {onToggleCollapsed && (
              <button
                type="button"
                className="icon-button data-collapse-toggle"
                onClick={onToggleCollapsed}
                title="작업 데이터 패널 최소화"
                aria-label="작업 데이터 패널 최소화"
                aria-expanded="true"
              >
                <PanelLeftClose size={17} />
              </button>
            )}
          </div>
        </div>
      )}

      {!collapsed && (selectedDataset ? (
        <>
          <div className="dataset-select-row">
            <div className="dataset-picker" ref={datasetPickerRef}>
              <button
                ref={datasetTriggerRef}
                type="button"
                className="dataset-select"
                role="combobox"
                aria-label="데이터셋 선택"
                aria-haspopup="listbox"
                aria-controls={datasetListId}
                aria-expanded={datasetMenuOpen}
                onClick={() => {
                  if (datasetMenuOpen) setDatasetMenuOpen(false)
                  else openDatasetMenu()
                }}
                onKeyDown={(event) => {
                  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
                  event.preventDefault()
                  setDatasetMenuOpen(true)
                  focusDatasetOption(event.key === 'ArrowDown' ? 'first' : 'last')
                }}
              >
                <span className="dataset-select-icon" aria-hidden="true">
                  <Database size={16} />
                </span>
                <span className="dataset-select-copy">
                  <strong>{selectedDataset.name}</strong>
                  <small>
                    {formatCount(selectedDataset.frame_count)} 프레임 · {DATASET_STATUS[selectedDataset.status]}
                  </small>
                </span>
                <ChevronDown
                  className={datasetMenuOpen ? 'dataset-select-chevron open' : 'dataset-select-chevron'}
                  size={16}
                  aria-hidden="true"
                />
              </button>
              {datasetMenuOpen && (
                <div
                  className="dataset-options"
                >
                  <div className="dataset-options-heading">
                    <span>작업 데이터</span>
                    <small>{datasets.length}개</small>
                  </div>
                  <div
                    id={datasetListId}
                    className="dataset-options-scroll"
                    role="listbox"
                    aria-label="작업 데이터 목록"
                    onKeyDown={handleDatasetListKeyDown}
                  >
                    {datasets.map((dataset) => {
                      const selected = dataset.id === selectedDataset.id
                      return (
                        <button
                          type="button"
                          role="option"
                          aria-selected={selected}
                          tabIndex={selected ? 0 : -1}
                          className={`dataset-option ${selected ? 'selected' : ''}`}
                          key={dataset.id}
                          onClick={() => {
                            if (!selected) onDatasetChange(dataset.id)
                            setDatasetMenuOpen(false)
                            datasetTriggerRef.current?.focus()
                          }}
                        >
                          <span className="dataset-option-icon" aria-hidden="true">
                            <Database size={15} />
                          </span>
                          <span className="dataset-option-copy">
                            <strong>{dataset.name}</strong>
                            <small>
                              {formatCount(dataset.frame_count)} 프레임
                              {dataset.point_count !== undefined
                                ? ` · ${formatCount(dataset.point_count)} pts`
                                : ''}
                            </small>
                          </span>
                          <span className={`dataset-option-state status-${dataset.status}`}>
                            <i className={`status-dot ${dataset.status}`} aria-hidden="true" />
                            <small>{DATASET_STATUS[dataset.status]}</small>
                            {selected && <Check size={14} aria-hidden="true" />}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>
            {onRemoveDataset && (
              <button
                type="button"
                className="icon-button dataset-remove-button"
                disabled={removingDataset}
                aria-label={`${selectedDataset.name} 작업 목록에서 제거`}
                title="원본 폴더는 보존하고 작업 목록에서만 제거"
                onClick={() => onRemoveDataset(selectedDataset)}
              >
                {removingDataset ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}
              </button>
            )}
          </div>

          <div className="dataset-health">
            <div className={`dataset-health-line status-${selectedDataset.status}`}>
              <span className={`status-dot ${selectedDataset.status}`} />
              <strong>{DATASET_STATUS[selectedDataset.status]}</strong>
              <span>{selectedDataset.crs ?? '좌표계 미지정'}</span>
            </div>
            <div className="dataset-stats">
              <span>
                <Layers3 size={14} />
                {formatCount(selectedDataset.point_count)} pts
              </span>
              <span>
                <Camera size={14} />
                {formatCount(selectedDataset.frame_count)}
              </span>
              <span>
                <LocateFixed size={14} />
                {formatDistance(selectedDataset.distance_m)}
              </span>
            </div>
          </div>

          <section className="track-section">
            <div className="section-label">
              <span>작업 구간</span>
              <small>{selectedDataset.tracks.length} tracks</small>
            </div>
            <div className="track-list">
              <button
                type="button"
                className={`track-row ${selectedTrack === '' ? 'active' : ''}`}
                onClick={() => onTrackChange('')}
              >
                <span className="track-rail all" />
                <span>
                  <strong>전체 구간</strong>
                  <small>{formatCount(selectedDataset.frame_count)} 프레임</small>
                </span>
                <Box size={15} />
              </button>
              {selectedDataset.tracks.map((track, index) => (
                <button
                  type="button"
                  key={track.id}
                  className={`track-row ${selectedTrack === track.id ? 'active' : ''}`}
                  onClick={() => onTrackChange(track.id)}
                >
                  <span
                    className="track-rail"
                    style={{ backgroundColor: TRACK_COLORS[index % TRACK_COLORS.length] }}
                  />
                  <span>
                    <strong>{track.name}</strong>
                    <small>
                      {formatCount(track.frame_count)} · {formatDistance(track.distance_m)}
                    </small>
                  </span>
                  <Gauge size={15} />
                </button>
              ))}
            </div>
          </section>

          <section className="dataset-range-section" aria-label="실행 프레임 범위">
            <div className="section-label">
              <span>실행 프레임 범위</span>
              <small>지도 또는 A/D 키로 현재 프레임 이동</small>
            </div>
            <div className="frame-range-picker compact">
              <div className="frame-range-inputs">
                <label>
                  <span>시작 프레임</span>
                  <input
                    type="number"
                    min={1}
                    max={frameLimit}
                    value={rangeDraft[0]}
                    placeholder="1"
                    aria-label="실행 시작 프레임 번호"
                    onChange={(event) =>
                      setRangeDraft((current) => [event.target.value, current[1]])
                    }
                    onKeyDown={(event) => event.key === 'Enter' && applyRangeDraft()}
                  />
                </label>
                <span aria-hidden="true">–</span>
                <label>
                  <span>끝 프레임</span>
                  <input
                    type="number"
                    min={1}
                    max={frameLimit}
                    value={rangeDraft[1]}
                    placeholder={String(frameLimit)}
                    aria-label="실행 끝 프레임 번호"
                    onChange={(event) =>
                      setRangeDraft((current) => [current[0], event.target.value])
                    }
                    onKeyDown={(event) => event.key === 'Enter' && applyRangeDraft()}
                  />
                </label>
                <button
                  type="button"
                  disabled={!rangeDraftValid}
                  onClick={applyRangeDraft}
                  title="입력한 프레임 범위를 작업 구간으로 적용"
                >
                  적용
                </button>
              </div>
              <div className="frame-range-actions" role="group" aria-label="실행 프레임 범위 지정">
                <button
                  type="button"
                  disabled={!selectedFrame}
                  aria-pressed={Boolean(
                    selectedFrame && frameRange?.[0] === selectedFrame.index,
                  )}
                  aria-label={
                    selectedFrame
                      ? `현재 프레임 ${selectedFrame.index + 1}을 실행 범위 시작으로 지정`
                      : '실행 범위 시작으로 지정할 프레임이 없습니다'
                  }
                  onClick={() => selectedFrame && onSetFrameRangeStart(selectedFrame.index)}
                >
                  시작 지정
                </button>
                <button
                  type="button"
                  disabled={!selectedFrame}
                  aria-pressed={Boolean(
                    selectedFrame && frameRange?.[1] === selectedFrame.index,
                  )}
                  aria-label={
                    selectedFrame
                      ? `현재 프레임 ${selectedFrame.index + 1}을 실행 범위 끝으로 지정`
                      : '실행 범위 끝으로 지정할 프레임이 없습니다'
                  }
                  onClick={() => selectedFrame && onSetFrameRangeEnd(selectedFrame.index)}
                >
                  끝 지정
                </button>
                <button
                  type="button"
                  disabled={!frameRange}
                  aria-label="실행 프레임 범위를 현재 작업 구간 전체로 초기화"
                  onClick={onClearFrameRange}
                >
                  <RotateCcw size={11} />
                  전체
                </button>
              </div>
              <div className="frame-range-row" aria-live="polite">
                <Flag size={13} />
                <span>
                  <small>실행 범위</small>
                  <strong>
                    {frameRange
                      ? `Frame ${String(frameRange[0] + 1).padStart(4, '0')}–${String(
                          frameRange[1] + 1,
                        ).padStart(4, '0')}`
                      : '현재 작업 구간 전체'}
                  </strong>
                </span>
                <code>
                  {frameRange ? `ordinal ${frameRange[0]}–${frameRange[1]}` : 'ALL'}
                </code>
              </div>
            </div>
          </section>

          <DatasetOverlayBrowser focusLayerId={focusOverlayLayerId} />
        </>
      ) : (
        <div className="panel-empty">
          <Database size={28} />
          <strong>선택된 데이터가 없습니다</strong>
          <p>서버 폴더를 스캔하거나 새 폴더를 업로드해 시작하세요.</p>
          <button type="button" className="button secondary full" onClick={onOpenSource}>
            데이터 연결
          </button>
        </div>
      ))}
    </aside>
  )
}
