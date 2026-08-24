import { AlertTriangle, CheckCircle2, Circle, MapPin } from 'lucide-react'
import type { ReviewTask, ReviewTaskStatus, ReviewTaskType } from '../types'
import { isReviewTaskComplete } from './ReviewContext'

const TASK_TYPE_LABELS: Readonly<Record<ReviewTaskType, string>> = {
  MANUAL_SCAN: '수동 확인',
  LOW_CONFIDENCE: '낮은 신뢰도',
  PROJECTION_FAILED: '3D 위치화 실패',
  GEOMETRY_REVIEW: '형상 재검토',
  POLE_BASE_REVIEW: '지주 하단 재검토',
  SPACING_ANOMALY: '간격 이상',
  UNREVIEWED_INTERVAL: '미검수 구간',
  MANUAL_FLAG: '나중에 확인',
}

const TASK_STATUS_LABELS: Readonly<Record<ReviewTaskStatus, string>> = {
  todo: '대기',
  in_progress: '검수 중',
  confirmed: '완료',
  corrected: '수정 완료',
  manual_added: '수동 추가',
  false_positive: '오검출',
  skipped: '건너뜀',
  field_survey: '현장조사',
}

export function reviewTaskTypeLabel(type: ReviewTaskType): string {
  return TASK_TYPE_LABELS[type]
}

export function reviewTaskStatusLabel(status: ReviewTaskStatus): string {
  return TASK_STATUS_LABELS[status]
}

export function reviewTaskEvidenceSummary(task: ReviewTask): string {
  const evidence = task.priority_evidence
  if (!evidence) return ''
  const parts: string[] = []
  if (typeof evidence.reason === 'string' && evidence.reason.trim()) {
    parts.push(evidence.reason.trim())
  }
  if (typeof evidence.source_weight === 'number' && Number.isFinite(evidence.source_weight)) {
    parts.push(`source 가중치 ${evidence.source_weight.toFixed(2)}`)
  }
  if (typeof evidence.adjustment === 'number' && Number.isFinite(evidence.adjustment)) {
    parts.push(`보정 ${evidence.adjustment >= 0 ? '+' : ''}${evidence.adjustment.toFixed(1)}`)
  }
  return parts.join(' · ')
}

export function reviewTaskFrameSummary(task: ReviewTask): string {
  if (
    typeof task.frame_start === 'number' &&
    typeof task.frame_end === 'number' &&
    task.frame_end >= task.frame_start
  ) {
    const count = task.frame_end - task.frame_start + 1
    const track = task.track_id ? `${task.track_id} · ` : ''
    return `${track}frame ${task.frame_start}–${task.frame_end} · ${count}개 프레임`
  }
  return task.frame_id ?? ''
}

export function ReviewTaskCard({
  task,
  current,
  busy,
  onSelect,
}: {
  task: ReviewTask
  current: boolean
  busy: boolean
  onSelect: () => void
}) {
  const complete = isReviewTaskComplete(task)
  const evidenceSummary = reviewTaskEvidenceSummary(task)
  const frameSummary = reviewTaskFrameSummary(task)
  return (
    <button
      type="button"
      className={`review-task-card ${current ? 'current' : ''} ${complete ? 'complete' : ''}`}
      aria-current={current ? 'true' : undefined}
      disabled={busy}
      title={evidenceSummary || undefined}
      onClick={onSelect}
    >
      <span className="review-task-state" aria-hidden="true">
        {complete ? (
          <CheckCircle2 size={15} />
        ) : task.status === 'in_progress' ? (
          <AlertTriangle size={15} />
        ) : (
          <Circle size={15} />
        )}
      </span>
      <span className="review-task-main">
        <span>
          <strong>{reviewTaskTypeLabel(task.task_type)}</strong>
          <em>{reviewTaskStatusLabel(task.status)}</em>
        </span>
        <small>
          {task.class_hint ?? '클래스 미지정'} · 우선순위 {Math.round(task.priority)}
        </small>
        {task.reason_codes.length > 0 && (
          <small className="review-task-reasons">{task.reason_codes.join(' · ')}</small>
        )}
        {evidenceSummary && (
          <small className="review-task-evidence">근거 · {evidenceSummary}</small>
        )}
        {frameSummary && (
          <small className="review-task-frame">
            <MapPin size={11} /> {frameSummary}
          </small>
        )}
      </span>
    </button>
  )
}
