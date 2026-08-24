import { CheckCheck, ClipboardCheck, Download, ListChecks, Pause, Play, Plus, RefreshCcw, Wand2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ReviewCandidateSources, ReviewSessionStatus } from '../types'
import { useOptionalReviewWorkspace } from './ReviewContext'
import { useOptionalOverlayWorkspace } from './OverlayContext'

const SESSION_STATUS_LABELS: Readonly<Record<ReviewSessionStatus, string>> = {
  draft: '초안',
  active: '진행 중',
  paused: '일시 정지',
  completed: '완료',
  archived: '보관됨',
}

export function ReviewSessionBar({
  activeLearningExportEnabled = false,
}: {
  activeLearningExportEnabled?: boolean
}) {
  const review = useOptionalReviewWorkspace()
  const overlay = useOptionalOverlayWorkspace()
  const [candidateOpen, setCandidateOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [sourceRunId, setSourceRunId] = useState('')
  const [createLayerId, setCreateLayerId] = useState('')
  const [sources, setSources] = useState<ReviewCandidateSources>({
    low_confidence: true,
    projection_failed: true,
    geometry_review: true,
    pole_base_review: true,
    unreviewed_interval: true,
    spacing_anomaly: false,
  })
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
  if (!review?.enabled || !review.datasetId) return null

  const percent = Math.round(review.progress * 100)
  const toggleSource = (name: keyof ReviewCandidateSources) => {
    setSources((current) => ({ ...current, [name]: !current[name] }))
  }
  return (
    <section className="review-session-bar" aria-label="검수 세션">
      <div className="review-session-title">
        <ClipboardCheck size={16} />
        <span>
          <strong>Review Workspace</strong>
          <small>J 다음 · K 이전</small>
        </span>
      </div>

      <label className="review-session-select">
        <span>검수 세션</span>
        <select
          aria-label="검수 세션 선택"
          value={review.session?.id ?? ''}
          disabled={review.loading || review.sessions.length === 0}
          onChange={(event) => review.selectSession(event.target.value)}
        >
          {review.sessions.length === 0 && <option value="">검수 세션 없음</option>}
          {review.sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.id} · {SESSION_STATUS_LABELS[session.status]}
            </option>
          ))}
        </select>
      </label>

      <div className="review-session-progress" aria-label={`검수 진행률 ${percent}%`}>
        <span>
          <strong>{review.completedCount.toLocaleString('ko-KR')}</strong>
          <small>/ {review.totalCount.toLocaleString('ko-KR')} 완료</small>
        </span>
        <progress max={Math.max(review.totalCount, 1)} value={review.completedCount} />
        <em>{percent}%</em>
      </div>

      {review.currentTask && (
        <span className="review-current-task">
          현재 {review.currentTaskIndex + 1} / {review.totalCount}
        </span>
      )}

      <div className="review-session-actions">
        <button
          type="button"
          className={`button compact ${createOpen ? 'primary' : 'secondary'}`}
          aria-expanded={createOpen}
          onClick={() => {
            setCreateOpen((value) => !value)
            setCandidateOpen(false)
          }}
        >
          <Plus size={14} /> 새 세션
        </button>
        {review.session?.status === 'active' && (
          <button
            type="button"
            className={`button compact ${candidateOpen ? 'primary' : 'secondary'}`}
            aria-expanded={candidateOpen}
            onClick={() => {
              setCandidateOpen((value) => !value)
              setCreateOpen(false)
            }}
          >
            <Wand2 size={14} /> 후보 생성
          </button>
        )}
        {review.session?.status === 'active' && (
          <button type="button" className="button compact secondary" disabled={review.updatingSession} onClick={() => void review.setSessionStatus('paused')}>
            <Pause size={14} /> 일시 정지
          </button>
        )}
        {review.session && ['paused', 'draft'].includes(review.session.status) && (
          <button type="button" className="button compact secondary" disabled={review.updatingSession} onClick={() => void review.setSessionStatus('active')}>
            <Play size={14} /> 재개
          </button>
        )}
        {review.session && ['active', 'paused'].includes(review.session.status) && (
          <button
            type="button"
            className="button compact secondary"
            disabled={review.loading || review.updatingSession || review.completedCount < review.totalCount}
            onClick={() => void review.completeSession()}
          >
            <CheckCheck size={14} /> 세션 완료
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
          <ListChecks size={14} /> 작업 큐
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label="검수 작업 새로고침"
          title="검수 작업 새로고침"
          disabled={review.loading}
          onClick={review.reload}
        >
          <RefreshCcw size={14} className={review.loading ? 'spin' : undefined} />
        </button>
      </div>
      {createOpen && (
        <div className="review-create-popover" role="dialog" aria-label="새 검수 세션 생성">
          <strong>현재 작업 범위로 새 세션</strong>
          <label>
            <span>완료 run</span>
            <select aria-label="검수 세션 원본 완료 run" value={sourceRunId} onChange={(event) => setSourceRunId(event.target.value)}>
              {review.sourceRuns.length === 0 && <option value="">완료 run 없음</option>}
              {review.sourceRuns.map((run) => <option key={run.id} value={run.id}>{run.label}</option>)}
            </select>
          </label>
          <label>
            <span>대상 Point 레이어</span>
            <select aria-label="새 검수 세션 대상 Point 레이어" value={createLayerId} onChange={(event) => setCreateLayerId(event.target.value)}>
              {pointLayers.length === 0 && <option value="">Point 레이어 없음</option>}
              {pointLayers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}
            </select>
          </label>
          <small>
            {review.activeFrame
              ? `${review.activeFrame.track_id} · frame ${review.frameRange ? `${review.frameRange[0]}–${review.frameRange[1]}` : review.activeFrame.index}`
              : '먼저 프레임을 선택하세요.'}
          </small>
          <button
            type="button"
            className="button primary compact"
            disabled={!review.activeFrame || !sourceRunId || !createLayerId || review.creatingSession}
            onClick={() => void review.createDefaultSession(createLayerId, [sourceRunId]).then(() => setCreateOpen(false))}
          >
            {review.creatingSession ? '생성 중…' : '선택 범위로 생성'}
          </button>
        </div>
      )}
      {candidateOpen && review.session?.status === 'active' && (
        <div className="review-candidate-popover" role="dialog" aria-label="검수 후보 생성">
          <strong>후보 source</strong>
          {([
            ['low_confidence', '낮은 신뢰도'],
            ['projection_failed', '3D 위치화 실패'],
            ['geometry_review', '형상 REVIEW'],
            ['pole_base_review', '지주 하단 REVIEW'],
            ['unreviewed_interval', '미검수 구간'],
            ['spacing_anomaly', '간격 이상'],
          ] as const).map(([name, label]) => (
            <label key={name}><input type="checkbox" checked={sources[name]} onChange={() => toggleSource(name)} /> {label}</label>
          ))}
          <button type="button" className="button primary compact" disabled={review.generatingCandidates || !Object.values(sources).some(Boolean)} onClick={() => void review.generateCandidates(sources).then(() => setCandidateOpen(false))}>
            {review.generatingCandidates ? '생성 중…' : '선택 source로 생성'}
          </button>
        </div>
      )}
    </section>
  )
}
