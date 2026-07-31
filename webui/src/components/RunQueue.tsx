import {
  Ban,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Clock3,
  Download,
  LoaderCircle,
  XCircle,
} from 'lucide-react'
import type { RunRecord, RunStatus } from '../types'
import { formatDate, formatDuration } from '../lib/format'

const STATUS: Record<RunStatus, { label: string; icon: typeof Clock3 }> = {
  queued: { label: '대기 중', icon: Clock3 },
  preparing: { label: '준비 중', icon: CircleDashed },
  running: { label: '실행 중', icon: LoaderCircle },
  completed: { label: '완료', icon: CheckCircle2 },
  failed: { label: '실패', icon: XCircle },
  cancelled: { label: '취소됨', icon: Ban },
  cancelling: { label: '취소 중', icon: LoaderCircle },
}

export function RunQueue({
  runs,
  open,
  onClose,
  onCancel,
}: {
  runs: RunRecord[]
  open: boolean
  onClose: () => void
  onCancel: (id: string) => void
}) {
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
            <strong>{runs.filter((run) => ['running', 'preparing'].includes(run.status)).length}</strong>
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
            <RunCard key={run.id} run={run} onCancel={onCancel} />
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
    </>
  )
}

function RunCard({ run, onCancel }: { run: RunRecord; onCancel: (id: string) => void }) {
  const meta = STATUS[run.status]
  const Icon = meta.icon
  const active = ['queued', 'preparing', 'running', 'cancelling'].includes(run.status)
  return (
    <article className={`run-card status-${run.status}`}>
      <div className="run-card-top">
        <span className="run-status">
          <Icon size={15} className={['running', 'cancelling'].includes(run.status) ? 'spin' : ''} />
          {meta.label}
        </span>
        <time>{formatDate(run.created_at)}</time>
      </div>
      <h3>{run.dataset_name ?? run.dataset_id}</h3>
      <p>{run.error ?? run.message ?? run.stage ?? '작업 요청을 준비하고 있습니다.'}</p>
      <div className="progress-track" aria-label={`진행률 ${Math.round(run.progress)}%`}>
        <span style={{ width: `${Math.max(0, Math.min(100, run.progress))}%` }} />
      </div>
      <div className="run-progress-meta">
        <strong>{Math.round(run.progress)}%</strong>
        {active && run.eta_seconds !== undefined && Number.isFinite(run.eta_seconds) && (
          <small>예상 {formatDuration(run.eta_seconds)}</small>
        )}
        {run.status === 'completed' && <small>처리 완료</small>}
      </div>
      <footer>
        <code>{run.id.slice(0, 18)}</code>
        {active && run.status !== 'cancelling' && (
          <button type="button" className="danger-action" onClick={() => onCancel(run.id)}>
            <Ban size={13} />
            취소
          </button>
        )}
        {run.status === 'completed' && run.result_url && (
          <a href={run.result_url} className="result-action">
            <Download size={13} />
            결과 받기
          </a>
        )}
      </footer>
    </article>
  )
}
