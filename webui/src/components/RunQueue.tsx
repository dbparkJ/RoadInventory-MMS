import {
  Ban,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Clock3,
  Download,
  LoaderCircle,
  Trash2,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import type { CanonicalRunStatus, RunRecord } from '../types'
import { formatDate, formatDuration } from '../lib/format'
import { RunResultsDialog } from './RunResultsDialog'

const STATUS = {
  queued: { label: '대기 중', icon: Clock3 },
  preparing: { label: '준비 중', icon: CircleDashed },
  starting: { label: '시작 중', icon: LoaderCircle },
  running: { label: '실행 중', icon: LoaderCircle },
  completed: { label: '완료', icon: CheckCircle2 },
  failed: { label: '실패', icon: XCircle },
  cancelled: { label: '취소됨', icon: Ban },
  cancelling: { label: '취소 중', icon: LoaderCircle },
}

const CANONICAL_STATUS: Record<CanonicalRunStatus, { label: string; icon: typeof Clock3 }> = {
  pending: STATUS.queued,
  validating: STATUS.preparing,
  running: STATUS.running,
  succeeded: STATUS.completed,
  failed: STATUS.failed,
  retrying: { label: '재시도 중', icon: LoaderCircle },
  cancelled: STATUS.cancelled,
}

const UNKNOWN_STATUS = { label: '알 수 없는 상태', icon: CircleDashed }
const ACTIVE_STATUSES = new Set(['queued', 'preparing', 'starting', 'running', 'cancelling'])
const ACTIVE_CANONICAL_STATUSES = new Set<CanonicalRunStatus>([
  'pending',
  'validating',
  'running',
  'retrying',
])

function statusMeta(run: RunRecord) {
  const canonical =
    run.canonical_status &&
    Object.prototype.hasOwnProperty.call(CANONICAL_STATUS, run.canonical_status)
      ? CANONICAL_STATUS[run.canonical_status]
      : undefined
  if (canonical) return canonical
  return Object.prototype.hasOwnProperty.call(STATUS, run.status)
    ? STATUS[run.status as keyof typeof STATUS]
    : UNKNOWN_STATUS
}

function isActive(run: RunRecord) {
  return (
    ACTIVE_STATUSES.has(run.status) ||
    (run.canonical_status !== undefined && ACTIVE_CANONICAL_STATUSES.has(run.canonical_status))
  )
}

function statusClass(status: string) {
  return Object.prototype.hasOwnProperty.call(STATUS, status) ? status : 'unknown'
}

function normalizedProgress(progress: number) {
  return Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0
}

export function RunQueue({
  runs,
  open,
  onClose,
  onCancel,
  onDelete,
}: {
  runs: RunRecord[]
  open: boolean
  onClose: () => void
  onCancel: (id: string) => void
  onDelete: (id: string) => void | Promise<void>
}) {
  const [resultRun, setResultRun] = useState<RunRecord | null>(null)
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null)

  const deleteRun = async (run: RunRecord) => {
    const ownerWindow = document.defaultView ?? window
    const confirmed = ownerWindow.confirm(
      `${run.dataset_name ?? run.dataset_id} 실행 기록을 목록에서 제거할까요?\n\n검출 산출물과 서버 파일은 삭제되지 않습니다.`,
    )
    if (!confirmed) return
    setDeletingRunId(run.id)
    try {
      await onDelete(run.id)
    } finally {
      setDeletingRunId((current) => (current === run.id ? null : current))
    }
  }
  return (
    <>
      {open && <button type="button" className="drawer-scrim" onClick={onClose} aria-label="실행 큐 닫기" />}
      <aside className={`run-drawer ${open ? 'open' : ''}`} aria-label="실행 큐" aria-hidden={!open}>
        <header>
          <div>
            <span className="eyebrow">SERVER QUEUE</span>
            <h2>실행 큐</h2>
          </div>
          <button type="button" className="icon-button" onClick={onClose}>
            <ChevronRight size={18} />
          </button>
        </header>
        <div className="queue-overview">
          <div>
            <strong>{runs.filter(isActive).length}</strong>
            <small>실행 중</small>
          </div>
          <div>
            <strong>{runs.filter((run) => run.status === 'queued').length}</strong>
            <small>대기</small>
          </div>
          <div>
            <strong>{runs.filter((run) => run.status === 'completed').length}</strong>
            <small>완료</small>
          </div>
        </div>
        <div className="run-list">
          {runs.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              deleting={deletingRunId === run.id}
              onCancel={onCancel}
              onDelete={() => void deleteRun(run)}
              onOpenResults={setResultRun}
            />
          ))}
          {!runs.length && (
            <div className="queue-empty">
              <CircleDashed size={29} />
              <strong>실행 기록이 없습니다</strong>
              <p>작업을 시작하면 상태와 진행률이 이곳에 표시됩니다.</p>
            </div>
          )}
        </div>
      </aside>
      <RunResultsDialog run={resultRun} onClose={() => setResultRun(null)} />
    </>
  )
}

function RunCard({
  run,
  deleting,
  onCancel,
  onDelete,
  onOpenResults,
}: {
  run: RunRecord
  deleting: boolean
  onCancel: (id: string) => void
  onDelete: () => void
  onOpenResults: (run: RunRecord) => void
}) {
  const meta = statusMeta(run)
  const Icon = meta.icon
  const active = isActive(run)
  const progress = normalizedProgress(run.progress)
  const spinning =
    ['starting', 'running', 'cancelling'].includes(run.status) ||
    run.canonical_status === 'retrying'
  return (
    <article className={`run-card status-${statusClass(run.status)}`}>
      <div className="run-card-top">
        <span className="run-status">
          <Icon size={15} className={spinning ? 'spin' : ''} />
          {meta.label}
        </span>
        <time>{formatDate(run.created_at)}</time>
      </div>
      <h3>{run.dataset_name ?? run.dataset_id}</h3>
      <p>
        {run.error_info?.message ??
          run.error ??
          run.message ??
          run.current_stage ??
          run.stage ??
          '작업 요청을 준비하고 있습니다.'}
      </p>
      <div className="progress-track" aria-label={`진행률 ${Math.round(progress)}%`}>
        <span style={{ width: `${progress}%` }} />
      </div>
      <div className="run-progress-meta">
        <strong>{Math.round(progress)}%</strong>
        {active && run.eta_seconds !== undefined && Number.isFinite(run.eta_seconds) && (
          <small>예상 {formatDuration(run.eta_seconds)}</small>
        )}
        {run.status === 'completed' && <small>처리 완료</small>}
      </div>
      <footer>
        <code>{run.id.slice(0, 18)}</code>
        <span className="run-card-actions">
          {active && run.status !== 'cancelling' && (
            <button type="button" className="danger-action" onClick={() => onCancel(run.id)}>
              <Ban size={13} />
              취소
            </button>
          )}
          {run.status === 'completed' && (
            <button type="button" className="result-action" onClick={() => onOpenResults(run)}>
              <Download size={13} />
              결과 보기·받기
            </button>
          )}
          {(run.status === 'completed' || run.status === 'failed') && (
            <button
              type="button"
              className="delete-run-action"
              disabled={deleting}
              aria-label={`${run.dataset_name ?? run.dataset_id} 실행 기록 삭제`}
              onClick={onDelete}
            >
              {deleting ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={13} />}
              {deleting ? '제거 중' : '삭제'}
            </button>
          )}
        </span>
      </footer>
    </article>
  )
}
