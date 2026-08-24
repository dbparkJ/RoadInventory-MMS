import {
  CheckCheck,
  ClipboardCheck,
  Download,
  ListChecks,
  Pause,
  Play,
  Plus,
  RefreshCcw,
  Wand2,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { ReviewCandidateSources, ReviewSession, ReviewSessionStatus } from '../types'
import {
  reviewCompletionBlockerMessages,
  useOptionalReviewWorkspace,
} from './ReviewContext'
import { useOptionalOverlayWorkspace } from './OverlayContext'

const SESSION_STATUS_LABELS: Readonly<Record<ReviewSessionStatus, string>> = {
  draft: '준비 중',
  active: '진행 중',
  paused: '일시 정지',
  completed: '완료',
  archived: '보관됨',
}

const CANDIDATE_SOURCE_OPTIONS = [
  ['low_confidence', '낮은 신뢰도'],
  ['projection_failed', '3D 위치화 실패'],
  ['geometry_review', '형상 재검토'],
  ['pole_base_review', '지주 하단 재검토'],
  ['unreviewed_interval', '미검수 구간'],
  ['spacing_anomaly', '간격 이상'],
] as const

const DEFAULT_CANDIDATE_SOURCES: ReviewCandidateSources = {
  low_confidence: true,
  projection_failed: true,
  geometry_review: true,
  pole_base_review: true,
  unreviewed_interval: true,
  spacing_anomaly: false,
}

const REVIEW_START_POPOVER_ID = 'review-start-popover'
const REVIEW_CANDIDATE_POPOVER_ID = 'review-candidate-popover'

function compactLabels(ids: string[], labels: ReadonlyMap<string, string>, empty: string): string {
  if (!ids.length) return empty
  const resolved = ids.map((id) => labels.get(id) ?? id)
  return resolved.length > 2
    ? `${resolved.slice(0, 2).join(', ')} 외 ${resolved.length - 2}개`
    : resolved.join(', ')
}

export function reviewSessionScopeSummary(
  session: ReviewSession,
  runLabels: ReadonlyMap<string, string> = new Map(),
  layerLabels: ReadonlyMap<string, string> = new Map(),
): { run: string; layer: string; location: string; option: string } {
  const run = compactLabels(session.source_run_ids, runLabels, '원본 run 전체')
  const layer = compactLabels(session.target_layer_ids, layerLabels, '작업에서 지정한 Point 레이어')
  const tracks = session.track_ids.length ? session.track_ids.join(', ') : '모든 track'
  const frames = session.frame_range
    ? `frame ${session.frame_range[0]}–${session.frame_range[1]}`
    : '전체 frame'
  const location = `${tracks} · ${frames}`
  return {
    run,
    layer,
    location,
    option: `${run} · ${layer} · ${location} · ${SESSION_STATUS_LABELS[session.status]}`,
  }
}

export function ReviewSessionBar({
  activeLearningExportEnabled = false,
}: {
  activeLearningExportEnabled?: boolean
}) {
  const review = useOptionalReviewWorkspace()
  const overlay = useOptionalOverlayWorkspace()
  const [sourceRunId, setSourceRunId] = useState('')
  const [createLayerId, setCreateLayerId] = useState('')
  const [sources, setSources] = useState<ReviewCandidateSources>(DEFAULT_CANDIDATE_SOURCES)

  useEffect(() => {
    const runs = review?.sourceRuns ?? []
    setSourceRunId((current) => runs.some((run) => run.id === current) ? current : runs[0]?.id ?? '')
  }, [review?.datasetId, review?.sourceRuns])
  const pointLayers = (overlay?.layers ?? []).filter((layer) => layer.geometry_type.toLowerCase() === 'point')
  useEffect(() => {
    setCreateLayerId((current) => pointLayers.some((layer) => layer.id === current)
      ? current
      : pointLayers.find((layer) => layer.id === overlay?.activeLayerId)?.id ?? pointLayers[0]?.id ?? '')
  }, [overlay?.activeLayerId, overlay?.layers]) // eslint-disable-line react-hooks/exhaustive-deps

  const runLabels = useMemo(
    () => new Map((review?.sourceRuns ?? []).map((run) => [run.id, run.label])),
    [review?.sourceRuns],
  )
  const layerLabels = useMemo(
    () => new Map(pointLayers.map((layer) => [layer.id, layer.name])),
    [pointLayers],
  )
  if (!review?.enabled || !review.datasetId) return null

  const percent = Math.round(review.progress * 100)
  const reviewWorkBusy = review.creatingSession || review.generatingCandidates
  const selectedSummary = review.session
    ? reviewSessionScopeSummary(review.session, runLabels, layerLabels)
    : null
  const phaseIndex = !review.session
    ? 0
    : review.session.status === 'completed' || review.session.status === 'archived'
      ? 4
      : review.totalCount === 0 && !review.session.qa_ran_at
        ? 1
        : review.completedCount < review.totalCount
          ? 2
          : 3
  const phases = ['범위', '후보', '처리', 'QA', '완료']
  const pendingTaskCount = Math.max(0, review.totalCount - review.completedCount)
  const completionMessages = pendingTaskCount > 0
    ? [`미처리 검수 항목 ${pendingTaskCount.toLocaleString('ko-KR')}개`]
    : review.completionStatus
      ? reviewCompletionBlockerMessages(review.completionStatus.blockers)
      : []
  const canComplete = Boolean(
    review.session &&
    ['active', 'paused'].includes(review.session.status) &&
    review.completedCount === review.totalCount &&
    review.completionStatus?.can_complete,
  )
  const toggleSource = (name: keyof ReviewCandidateSources) => {
    setSources((current) => ({ ...current, [name]: !current[name] }))
  }
  const closeGuides = () => {
    review.setStartGuideOpen(false)
    review.setCandidateGuideOpen(false)
  }

  return (
    <section className="review-session-bar" aria-label="검수 작업">
      <div className="review-work-main">
        <div className="review-session-title">
          <ClipboardCheck size={16} />
          <span>
            <strong>검수 작업</strong>
            <small>후보를 확인하고 결과를 기록하는 작업 묶음</small>
          </span>
        </div>

        <label className="review-session-select">
          <span>작업 묶음 선택</span>
          <select
            aria-label="검수 작업 선택"
            value={review.session?.id ?? ''}
            disabled={review.loading || reviewWorkBusy || review.sessions.length === 0}
            onChange={(event) => review.selectSession(event.target.value)}
          >
            {review.sessions.length === 0 && <option value="">시작한 검수 작업 없음</option>}
            {review.sessions.map((session) => (
              <option key={session.id} value={session.id}>
                {reviewSessionScopeSummary(session, runLabels, layerLabels).option}
              </option>
            ))}
          </select>
        </label>

        <div className="review-session-progress" aria-label={`검수 진행률 ${percent}%`}>
          <span>
            <strong>{review.completedCount.toLocaleString('ko-KR')}</strong>
            <small>/ {review.totalCount.toLocaleString('ko-KR')} 처리</small>
          </span>
          <progress max={Math.max(review.totalCount, 1)} value={review.completedCount} />
          <em>{percent}%</em>
        </div>

        <div className="review-session-actions">
          <button
            type="button"
            className={`button compact ${review.startGuideOpen ? 'primary' : 'secondary'}`}
            aria-expanded={review.startGuideOpen}
            aria-controls={REVIEW_START_POPOVER_ID}
            disabled={reviewWorkBusy}
            onClick={() => {
              const next = !review.startGuideOpen
              closeGuides()
              review.setStartGuideOpen(next)
            }}
          >
            <Plus size={14} /> 새 검수 작업
          </button>
          {review.session?.status === 'active' && (
            <button
              type="button"
              className={`button compact ${review.candidateGuideOpen ? 'primary' : 'secondary'}`}
              aria-expanded={review.candidateGuideOpen}
              aria-controls={REVIEW_CANDIDATE_POPOVER_ID}
              disabled={reviewWorkBusy}
              onClick={() => {
                const next = !review.candidateGuideOpen
                closeGuides()
                review.setCandidateGuideOpen(next)
              }}
            >
              <Wand2 size={14} /> 후보 추가
            </button>
          )}
          {review.session?.status === 'active' && (
            <button type="button" className="button compact secondary" disabled={review.updatingSession || reviewWorkBusy} onClick={() => void review.setSessionStatus('paused')}>
              <Pause size={14} /> 일시 정지
            </button>
          )}
          {review.session && ['paused', 'draft'].includes(review.session.status) && (
            <button type="button" className="button compact secondary" disabled={review.updatingSession || reviewWorkBusy} onClick={() => void review.setSessionStatus('active')}>
              <Play size={14} /> 재개
            </button>
          )}
          {review.session && ['active', 'paused'].includes(review.session.status) && (
            <button
              type="button"
              className="button compact secondary"
              disabled={review.loading || review.updatingSession || reviewWorkBusy || review.checkingCompletion || !canComplete}
              title={completionMessages.join(' · ') || undefined}
              onClick={() => void review.completeSession()}
            >
              <CheckCheck size={14} /> 검수 작업 완료
            </button>
          )}
          {review.session && (
            <details className="review-export-menu">
              <summary className="button compact secondary"><Download size={14} /> 결과 받기</summary>
              <div>
                <a href={api.reviewReportUrl(review.session.id, 'json')} download>보고서 JSON</a>
                <a href={api.reviewReportUrl(review.session.id, 'csv')} download>보고서 CSV</a>
                <a href={api.reviewReportUrl(review.session.id, 'markdown')} download>보고서 Markdown</a>
                <a href={api.reviewExportUrl(review.session.id)} download>편집 결과 ZIP</a>
                {activeLearningExportEnabled && (
                  <a href={api.reviewActiveLearningExportUrl(review.session.id)} download>Active-learning ZIP</a>
                )}
              </div>
            </details>
          )}
          <button
            type="button"
            className={`button compact ${review.queueOpen ? 'primary' : 'secondary'}`}
            aria-pressed={review.queueOpen}
            onClick={() => review.setQueueOpen(!review.queueOpen)}
          >
            <ListChecks size={14} /> 항목 목록
          </button>
          <button
            type="button"
            className="icon-button"
            aria-label="검수 작업 새로고침"
            title="검수 작업 새로고침"
            disabled={review.loading || reviewWorkBusy}
            onClick={review.reload}
          >
            <RefreshCcw size={14} className={review.loading ? 'spin' : undefined} />
          </button>
        </div>
      </div>

      <div className="review-work-meta">
        <ol className="review-work-steps" aria-label="검수 작업 단계">
          {phases.map((phase, index) => (
            <li
              key={phase}
              className={index < phaseIndex ? 'done' : index === phaseIndex ? 'current' : ''}
              aria-current={index === phaseIndex ? 'step' : undefined}
            >
              <span>{index + 1}</span>{phase}
            </li>
          ))}
        </ol>
        {selectedSummary ? (
          <p className="review-work-scope" title={`${selectedSummary.run} · ${selectedSummary.layer} · ${selectedSummary.location}`}>
            <span>원본 <strong>{selectedSummary.run}</strong></span>
            <span>대상 <strong>{selectedSummary.layer}</strong></span>
            <span>범위 <strong>{selectedSummary.location}</strong></span>
          </p>
        ) : (
          <p className="review-work-scope empty">현재 프레임과 범위를 기준으로 새 검수 작업을 시작하세요.</p>
        )}
        {review.session && ['active', 'paused'].includes(review.session.status) && (
          <div className={`review-completion-gate ${review.completionStatus?.can_complete ? 'ready' : ''}`} role="status">
            {review.checkingCompletion
              ? '완료 조건 확인 중…'
              : review.completionStatus?.can_complete
                ? 'QA 통과 · 검수 작업을 완료할 수 있습니다.'
                : completionMessages.length
                  ? completionMessages.join(' · ')
                  : (
                    <button type="button" onClick={() => void review.refreshCompletionStatus()}>
                      완료 조건 다시 확인
                    </button>
                  )}
          </div>
        )}
        {review.session && ['completed', 'archived'].includes(review.session.status) && (
          <div className="review-completion-gate ready" role="status">
            완료된 검수 작업 · 결과 받기에서 보고서와 편집 결과를 내려받을 수 있습니다.
          </div>
        )}
      </div>

      <div
        id={REVIEW_START_POPOVER_ID}
        className="review-start-popover"
        role="region"
        aria-label="새 검수 작업 시작"
        hidden={!review.startGuideOpen}
      >
        <header>
          <strong>새 검수 작업 시작</strong>
          <small>선택 범위와 후보 유형을 한 번에 설정합니다.</small>
        </header>
        <section>
          <b><span>1</span> 작업 범위</b>
          <p>
            {review.activeFrame
              ? `${review.activeFrame.track_id} · frame ${review.frameRange ? `${review.frameRange[0]}–${review.frameRange[1]}` : review.activeFrame.index}`
              : '먼저 파노라마 또는 지도에서 프레임을 선택하세요.'}
          </p>
        </section>
        <section>
          <b><span>2</span> 원본과 저장 대상</b>
          <label>
            <span>분석 결과 run</span>
            <select aria-label="새 검수 작업 원본 완료 run" value={sourceRunId} onChange={(event) => setSourceRunId(event.target.value)}>
              {review.sourceRuns.length === 0 && <option value="">완료 run 없음</option>}
              {review.sourceRuns.map((run) => <option key={run.id} value={run.id}>{run.label}</option>)}
            </select>
          </label>
          <label>
            <span>수정할 Point 레이어</span>
            <select aria-label="새 검수 작업 대상 Point 레이어" value={createLayerId} onChange={(event) => setCreateLayerId(event.target.value)}>
              {pointLayers.length === 0 && <option value="">Point 레이어 없음</option>}
              {pointLayers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}
            </select>
          </label>
        </section>
        <section>
          <b><span>3</span> 만들 후보</b>
          <div className="review-source-grid">
            {CANDIDATE_SOURCE_OPTIONS.map(([name, label]) => (
              <label key={name}><input type="checkbox" checked={sources[name]} onChange={() => toggleSource(name)} /> {label}</label>
            ))}
          </div>
        </section>
        <footer>
          <button type="button" className="button secondary compact" onClick={closeGuides}>취소</button>
          <button
            type="button"
            className="button primary compact"
            disabled={
              !review.activeFrame ||
              !sourceRunId ||
              !createLayerId ||
              review.creatingSession ||
              review.generatingCandidates ||
              !Object.values(sources).some(Boolean)
            }
            onClick={() => void review.startReviewWork(createLayerId, [sourceRunId], sources)}
          >
            {review.creatingSession || review.generatingCandidates ? '작업 준비 중…' : '검수 작업 시작'}
          </button>
        </footer>
      </div>
      {review.session?.status === 'active' && (
        <div
          id={REVIEW_CANDIDATE_POPOVER_ID}
          className="review-candidate-popover"
          role="region"
          aria-label="검수 후보 추가"
          hidden={!review.candidateGuideOpen}
        >
          <strong>현재 작업에 후보 추가</strong>
          <small>이미 존재하는 후보는 중복 생성하지 않습니다.</small>
          {CANDIDATE_SOURCE_OPTIONS.map(([name, label]) => (
            <label key={name}><input type="checkbox" checked={sources[name]} onChange={() => toggleSource(name)} /> {label}</label>
          ))}
          <footer>
            <button type="button" className="button secondary compact" onClick={closeGuides}>취소</button>
            <button
              type="button"
              className="button primary compact"
              disabled={review.generatingCandidates || !Object.values(sources).some(Boolean)}
              onClick={() => void review.generateCandidates(sources).then((created) => {
                if (created) review.setCandidateGuideOpen(false)
              })}
            >
              {review.generatingCandidates ? '후보 준비 중…' : '선택한 후보 추가'}
            </button>
          </footer>
        </div>
      )}
    </section>
  )
}
