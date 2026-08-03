import {
  AlertCircle,
  ArrowLeftRight,
  Bell,
  CheckCircle2,
  CircleDashed,
  CircleHelp,
  Clock3,
  Database,
  Download,
  Keyboard,
  ListChecks,
  MousePointer2,
  X,
} from 'lucide-react'
import { useEffect, useRef, type ReactNode } from 'react'
import { formatDate } from '../lib/format'
import type { RunRecord, RunStatus } from '../types'
import type { Toast } from './ToastRegion'

const RUN_STATUS: Record<RunStatus, { label: string; tone: string }> = {
  queued: { label: '대기 중', tone: 'pending' },
  preparing: { label: '준비 중', tone: 'pending' },
  running: { label: '실행 중', tone: 'active' },
  cancelling: { label: '취소 중', tone: 'pending' },
  completed: { label: '완료', tone: 'success' },
  failed: { label: '실패', tone: 'error' },
  cancelled: { label: '취소됨', tone: 'muted' },
}

interface PanelChromeProps {
  detached: boolean
  externalAction?: ReactNode
  onClose: () => void
}

interface ActivityPanelProps extends PanelChromeProps {
  runs: RunRecord[]
  alerts: Toast[]
  onOpenQueue: () => void
}

export function ActivityPanel({
  runs,
  alerts,
  detached,
  externalAction,
  onClose,
  onOpenQueue,
}: ActivityPanelProps) {
  const activeCount = runs.filter((run) =>
    ['queued', 'preparing', 'running', 'cancelling'].includes(run.status),
  ).length
  const failedCount = runs.filter((run) => run.status === 'failed').length
  const completedCount = runs.filter((run) => run.status === 'completed').length

  return (
    <UtilityPanelShell
      id="activity-panel-title"
      eyebrow="ACTIVITY CENTER"
      title="알림"
      icon={<Bell size={18} />}
      detached={detached}
      externalAction={externalAction}
      onClose={onClose}
    >
      <div className="activity-summary" aria-label="작업 상태 요약">
        <StatusSummary value={activeCount} label="진행 중" tone="active" />
        <StatusSummary value={completedCount} label="완료" tone="success" />
        <StatusSummary value={failedCount} label="확인 필요" tone="error" />
      </div>

      <section className="utility-section">
        <div className="utility-section-heading">
          <div>
            <span>최근 알림</span>
            <small>현재 세션</small>
          </div>
        </div>
        <div className="activity-list" aria-live="polite">
          {alerts.slice(0, 6).map((alert) => (
            <article className={`activity-alert tone-${alert.tone}`} key={alert.id}>
              {alert.tone === 'error' ? (
                <AlertCircle size={16} />
              ) : alert.tone === 'success' ? (
                <CheckCircle2 size={16} />
              ) : (
                <CircleHelp size={16} />
              )}
              <div>
                <strong>{alert.title}</strong>
                {alert.message && <p>{alert.message}</p>}
              </div>
            </article>
          ))}
          {!alerts.length && (
            <div className="utility-empty compact">
              <Bell size={21} />
              <span>새 알림이 없습니다.</span>
            </div>
          )}
        </div>
      </section>

      <section className="utility-section utility-runs">
        <div className="utility-section-heading">
          <div>
            <span>최근 작업</span>
            <small>{runs.length.toLocaleString('ko-KR')}건</small>
          </div>
          <button
            type="button"
            className="button ghost compact utility-queue-link"
            onClick={onOpenQueue}
          >
            <ListChecks size={14} /> 실행 큐 열기
          </button>
        </div>
        <div className="activity-list">
          {runs.slice(0, 8).map((run) => {
            const status = RUN_STATUS[run.status]
            return (
              <article className={`activity-run tone-${status.tone}`} key={run.id}>
                <span className="activity-run-state">
                  {run.status === 'completed' ? (
                    <CheckCircle2 size={16} />
                  ) : run.status === 'failed' ? (
                    <AlertCircle size={16} />
                  ) : run.status === 'running' ? (
                    <CircleDashed size={16} className="spin" />
                  ) : (
                    <Clock3 size={16} />
                  )}
                </span>
                <div>
                  <header>
                    <strong>{run.dataset_name ?? run.dataset_id}</strong>
                    <time>{formatDate(run.created_at)}</time>
                  </header>
                  <p>{run.error ?? run.message ?? run.stage ?? status.label}</p>
                  <div className="activity-run-progress">
                    <span style={{ width: `${Math.max(0, Math.min(100, run.progress))}%` }} />
                  </div>
                  <small>
                    {status.label} · {Math.round(run.progress)}%
                  </small>
                </div>
                {run.status === 'completed' && (
                  <button type="button" onClick={onOpenQueue} title="결과 보기" aria-label={`${run.dataset_name ?? run.dataset_id} 결과 보기`}>
                    <Download size={15} />
                  </button>
                )}
              </article>
            )
          })}
          {!runs.length && (
            <div className="utility-empty">
              <CircleDashed size={24} />
              <strong>아직 작업 기록이 없습니다</strong>
              <span>검출을 실행하면 진행 상태와 결과가 이곳에 표시됩니다.</span>
            </div>
          )}
        </div>
      </section>
    </UtilityPanelShell>
  )
}

export function HelpPanel({
  detached,
  externalAction,
  onClose,
}: PanelChromeProps) {
  return (
    <UtilityPanelShell
      id="help-panel-title"
      eyebrow="OPERATOR GUIDE"
      title="도움말"
      icon={<CircleHelp size={18} />}
      detached={detached}
      externalAction={externalAction}
      onClose={onClose}
    >
      <section className="help-intro">
        <strong>빠른 작업 순서</strong>
        <ol>
          <li><span>1</span> 데이터 폴더를 등록하고 인덱싱 완료를 확인합니다.</li>
          <li><span>2</span> 전체 구간 또는 한 트랙을 선택하고 프레임 범위를 지정합니다.</li>
          <li><span>3</span> 지도·파노라마·3D 포인트로 대상 위치를 검토합니다.</li>
          <li><span>4</span> 작업 설정에서 수동 입력 또는 자동 최적화 후 검출을 실행합니다.</li>
          <li><span>5</span> 알림이나 실행 큐에서 완료 상태와 결과 다운로드를 확인합니다.</li>
        </ol>
      </section>

      <section className="utility-section">
        <div className="utility-section-heading">
          <div><span>프레임 단축키</span><small>입력 칸에서는 작동하지 않음</small></div>
        </div>
        <dl className="shortcut-grid">
          <div><dt><kbd>A</kbd><kbd>←</kbd></dt><dd>이전 프레임</dd></div>
          <div><dt><kbd>D</kbd><kbd>→</kbd></dt><dd>다음 프레임</dd></div>
          <div><dt><kbd>P</kbd></dt><dd>선택한 SHP 피처의 실제 좌표 지정</dd></div>
          <div><dt><kbd>Esc</kbd></dt><dd>좌표 지정 또는 열린 창 닫기</dd></div>
        </dl>
      </section>

      <section className="help-card-grid">
        <article>
          <MousePointer2 size={18} />
          <div><strong>화면 탐색</strong><p>지도 점을 클릭해 프레임으로 이동하고, 뷰어에서는 드래그와 휠로 시점과 확대를 조절합니다.</p></div>
        </article>
        <article>
          <ArrowLeftRight size={18} />
          <div><strong>듀얼 모니터</strong><p>새 창 아이콘으로 컴포넌트를 분리하고 기본 화면 복귀 버튼으로 다시 합칠 수 있습니다.</p></div>
        </article>
        <article>
          <Keyboard size={18} />
          <div><strong>범위 지정</strong><p>프레임 행을 Shift+클릭하거나 시작·끝 번호를 입력해 검출 범위를 좁힐 수 있습니다.</p></div>
        </article>
        <article>
          <Database size={18} />
          <div><strong>원본 보호</strong><p>작업 데이터 제거는 등록과 프레임 인덱스만 해제하며 원본 폴더는 삭제하지 않습니다.</p></div>
        </article>
      </section>
    </UtilityPanelShell>
  )
}

function StatusSummary({ value, label, tone }: { value: number; label: string; tone: string }) {
  return (
    <div className={`tone-${tone}`}>
      <strong>{value.toLocaleString('ko-KR')}</strong>
      <span>{label}</span>
    </div>
  )
}

function UtilityPanelShell({
  id,
  eyebrow,
  title,
  icon,
  detached,
  externalAction,
  onClose,
  children,
}: PanelChromeProps & {
  id: string
  eyebrow: string
  title: string
  icon: ReactNode
  children: ReactNode
}) {
  const panelRef = useRef<HTMLElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    panel.focus()
    const ownerWindow = panel.ownerDocument.defaultView ?? window
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onCloseRef.current()
    }
    ownerWindow.addEventListener('keydown', onKeyDown)
    return () => ownerWindow.removeEventListener('keydown', onKeyDown)
  }, [detached])

  return (
    <section
      ref={panelRef}
      className="utility-panel"
      role="dialog"
      aria-modal={detached ? undefined : true}
      aria-labelledby={id}
      tabIndex={-1}
    >
      <header className="utility-panel-header">
        <span className="utility-panel-icon">{icon}</span>
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h2 id={id}>{title}</h2>
        </div>
        <div className="utility-panel-actions">
          {externalAction}
          <button type="button" className="icon-button" onClick={onClose} aria-label={`${title} 닫기`}>
            <X size={17} />
          </button>
        </div>
      </header>
      <div className="utility-panel-body">{children}</div>
    </section>
  )
}
