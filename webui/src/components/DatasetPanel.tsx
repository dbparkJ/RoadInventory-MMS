import {
  Box,
  Camera,
  ChevronDown,
  Cloud,
  Database,
  Flag,
  Gauge,
  Layers3,
  LocateFixed,
  LoaderCircle,
  RotateCcw,
  Search,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import type { DatasetSummary, Frame, FrameRange } from '../types'
import { formatCount, formatDistance, formatFrameTimestamp } from '../lib/format'

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
  onDatasetChange: (id: string) => void
  onTrackChange: (id: string) => void
  onFrameChange: (frame: Frame) => void
  onSetFrameRangeStart: (ordinal: number) => void
  onSetFrameRangeEnd: (ordinal: number) => void
  onClearFrameRange: () => void
  onLoadMoreFrames: () => void
  onOpenSource: () => void
}

export function DatasetPanel({
  datasets,
  selectedDataset,
  selectedTrack,
  frames,
  selectedFrame,
  framesLoading,
  framesLoadingMore,
  frameTotal,
  hasMoreFrames,
  frameRange,
  onDatasetChange,
  onTrackChange,
  onFrameChange,
  onSetFrameRangeStart,
  onSetFrameRangeEnd,
  onClearFrameRange,
  onLoadMoreFrames,
  onOpenSource,
}: DatasetPanelProps) {
  const [query, setQuery] = useState('')
  const visibleFrames = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return frames
    return frames.filter(
      (frame) =>
        frame.id.toLowerCase().includes(normalized) ||
        String(frame.index + 1).includes(normalized),
    )
  }, [frames, query])

  return (
    <aside className="data-panel" aria-label="데이터 탐색기">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">DATA EXPLORER</span>
          <h2>작업 데이터</h2>
        </div>
        <button type="button" className="icon-button" onClick={onOpenSource} title="데이터 추가">
          <Cloud size={17} />
        </button>
      </div>

      {selectedDataset ? (
        <>
          <label className="select-shell dataset-select">
            <Database size={16} />
            <select
              aria-label="데이터셋 선택"
              value={selectedDataset.id}
              onChange={(event) => onDatasetChange(event.target.value)}
            >
              {datasets.map((dataset) => (
                <option value={dataset.id} key={dataset.id}>
                  {dataset.name}
                </option>
              ))}
            </select>
            <ChevronDown size={15} />
          </label>

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
                  <span className={`track-rail color-${index % 4}`} />
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

          <section className="frame-section">
            <div className="section-label">
              <span>프레임</span>
              <small>
                {frames.length.toLocaleString('ko-KR')} / {frameTotal.toLocaleString('ko-KR')} loaded
              </small>
            </div>
            <label className="search-box">
              <Search size={14} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="불러온 프레임에서 번호 또는 ID 검색"
                aria-label="프레임 검색"
              />
            </label>
            {query && (
              <small className="search-scope-note">
                현재 불러온 {frames.length.toLocaleString('ko-KR')}개 기준 · 필요하면 아래에서 더 불러오세요.
              </small>
            )}
            <div className="frame-range-picker">
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
            <div className="frame-list" aria-busy={framesLoading}>
              {framesLoading && !frames.length
                ? Array.from({ length: 5 }, (_, index) => (
                    <div className="frame-skeleton" key={index}>
                      <span />
                      <div>
                        <i />
                        <i />
                      </div>
                    </div>
                  ))
                : visibleFrames.map((frame) => (
                    <button
                      type="button"
                      key={frame.id}
                      className={`frame-row ${selectedFrame?.id === frame.id ? 'active' : ''}`}
                      onClick={() => onFrameChange(frame)}
                    >
                      <span className="frame-index">{String(frame.index + 1).padStart(4, '0')}</span>
                      <span className="frame-meta">
                        <strong>
                          {formatFrameTimestamp(frame.timestamp)}
                        </strong>
                        <small>{frame.id}</small>
                      </span>
                      <span className="frame-assets">
                        {frame.has_panorama && <Camera size={13} />}
                        {frame.has_points && <Layers3 size={13} />}
                      </span>
                    </button>
                  ))}
              {hasMoreFrames && (
                <button
                  type="button"
                  className="frame-load-more"
                  disabled={framesLoadingMore}
                  onClick={onLoadMoreFrames}
                >
                  {framesLoadingMore ? <LoaderCircle size={14} className="spin" /> : <ChevronDown size={14} />}
                  {framesLoadingMore ? '프레임 불러오는 중' : '프레임 더 불러오기'}
                  <small>다음 240개</small>
                </button>
              )}
              {!hasMoreFrames && frames.length > 0 && (
                <div className="frame-list-end">전체 프레임을 불러왔습니다.</div>
              )}
              {!framesLoading && !visibleFrames.length && (
                <div className="list-empty">조건에 맞는 프레임이 없습니다.</div>
              )}
            </div>
          </section>
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
      )}
    </aside>
  )
}
