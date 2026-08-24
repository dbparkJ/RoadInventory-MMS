import { AlertCircle, CheckCircle2, LoaderCircle, Play, RefreshCcw, ShieldCheck, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { isTextEntryTarget } from '../lib/frameNavigation'
import type { QaIssue, QaIssueSeverity, QaIssueStatus } from '../types'
import { useOptionalOverlayWorkspace } from './OverlayContext'
import { useOptionalReviewWorkspace } from './ReviewContext'

const SEVERITY_LABELS: Record<QaIssueSeverity, string> = {
  error: '오류',
  warning: '경고',
  info: '안내',
}

export function QaIssuePanel() {
  const review = useOptionalReviewWorkspace()
  const overlay = useOptionalOverlayWorkspace()
  const [open, setOpen] = useState(false)
  const [issues, setIssues] = useState<QaIssue[]>([])
  const [total, setTotal] = useState(0)
  const [nextOffset, setNextOffset] = useState<number | null>(null)
  const [statusFilter, setStatusFilter] = useState<QaIssueStatus>('open')
  const [severityFilter, setSeverityFilter] = useState<QaIssueSeverity | ''>('')
  const [selectedIssueId, setSelectedIssueId] = useState('')
  const [overrideReason, setOverrideReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [updating, setUpdating] = useState(false)
  const [error, setError] = useState('')
  const requestRef = useRef(0)
  const selectedIssue = issues.find((issue) => issue.id === selectedIssueId) ?? null
  const enabled = Boolean(review?.enabled && review.session)

  const loadIssues = useCallback(async (offset = 0, append = false) => {
    if (!review?.session) return
    const requestId = requestRef.current + 1
    requestRef.current = requestId
    setLoading(true)
    setError('')
    if (!append) setNextOffset(null)
    try {
      const response = await api.qaIssues(
        review.session.id,
        {
          offset,
          limit: 200,
          status: statusFilter,
          severity: severityFilter || undefined,
        },
      )
      if (requestRef.current !== requestId) return
      setIssues((current) => append
        ? [...current, ...response.items.filter((issue) => !current.some((known) => known.id === issue.id))]
        : response.items)
      setTotal(response.total)
      setNextOffset(response.next_offset)
      if (!append) {
        setSelectedIssueId((current) =>
          response.items.some((issue) => issue.id === current) ? current : '',
        )
      }
    } catch (reason) {
      if (requestRef.current !== requestId) return
      setError(reason instanceof Error ? reason.message : 'QA 오류를 불러오지 못했습니다.')
    } finally {
      if (requestRef.current === requestId) setLoading(false)
    }
  }, [review?.session, severityFilter, statusFilter])

  useEffect(() => {
    if (!open || !enabled) return
    void loadIssues()
  }, [enabled, loadIssues, open])

  useEffect(() => {
    if (enabled) return
    requestRef.current += 1
    setOpen(false)
    setIssues([])
    setTotal(0)
    setNextOffset(null)
  }, [enabled])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.code !== 'KeyQ' ||
        isTextEntryTarget(event.target) ||
        !enabled
      ) return
      event.preventDefault()
      setOpen((value) => !value)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [enabled])

  const runQa = async () => {
    if (!review?.session || running) return
    setRunning(true)
    setError('')
    try {
      await api.runQa(review.session.id)
      await loadIssues()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'QA 검사를 실행하지 못했습니다.')
    } finally {
      setRunning(false)
    }
  }

  const navigateIssue = async (issue: QaIssue) => {
    setSelectedIssueId(issue.id)
    setOverrideReason(issue.override_reason ?? '')
    if (issue.feature_id && overlay) {
      overlay.selectFeature(
        { layerId: issue.layer_id, featureId: issue.feature_id },
        { navigate: !issue.frame_id },
      )
    }
    if (issue.frame_id) await review?.navigateFrame(issue.frame_id)
  }

  const patchIssue = async (status: QaIssueStatus) => {
    if (!selectedIssue || updating) return
    if (selectedIssue.severity === 'error' && status !== 'open') {
      setError('오류는 데이터를 수정한 뒤 QA 검사를 다시 실행해야 해소됩니다.')
      return
    }
    const reason = overrideReason.trim()
    if (status === 'dismissed' && reason.length < 3) {
      setError('경고를 무시하려면 3자 이상의 사유를 입력해 주세요.')
      return
    }
    setUpdating(true)
    setError('')
    try {
      await api.patchQaIssue(selectedIssue.id, {
        status,
        ...(status === 'dismissed' ? { override_reason: reason } : {}),
      })
      await loadIssues()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'QA 상태를 저장하지 못했습니다.')
    } finally {
      setUpdating(false)
    }
  }

  if (!review?.enabled) return null
  if (!open) {
    return (
      <button type="button" className="qa-panel-launcher" aria-label="QA 오류 탐색기 열기" title="QA 오류 탐색기 (Q)" disabled={!enabled} onClick={() => setOpen(true)}>
        <ShieldCheck size={15} /> QA
      </button>
    )
  }

  return (
    <aside className="qa-issue-panel" aria-label="QA 오류 탐색기">
      <header>
        <span><ShieldCheck size={15} /><strong>QA 오류 탐색기</strong><small>{total}</small></span>
        <div>
          <button type="button" aria-label="QA 검사 실행" title="QA 검사 실행" disabled={running || !enabled} onClick={() => void runQa()}>{running ? <LoaderCircle className="spin" size={13} /> : <Play size={13} />}</button>
          <button type="button" aria-label="QA 목록 새로고침" title="새로고침" disabled={loading || !enabled} onClick={() => void loadIssues()}><RefreshCcw className={loading ? 'spin' : ''} size={13} /></button>
          <button type="button" aria-label="QA 오류 탐색기 닫기" onClick={() => setOpen(false)}><X size={14} /></button>
        </div>
      </header>
      <div className="qa-filters">
        <select aria-label="QA 상태 필터" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as QaIssueStatus)}>
          <option value="open">미해결</option>
          <option value="resolved">해결됨</option>
          <option value="dismissed">무시됨</option>
        </select>
        <select aria-label="QA 심각도 필터" value={severityFilter} onChange={(event) => setSeverityFilter(event.target.value as QaIssueSeverity | '')}>
          <option value="">모든 심각도</option>
          <option value="error">오류</option>
          <option value="warning">경고</option>
          <option value="info">안내</option>
        </select>
      </div>
      {error && <p className="qa-error" role="alert">{error}</p>}
      <div className="qa-issue-list">
        {loading && issues.length === 0 && <p>QA 오류를 불러오는 중입니다.</p>}
        {!loading && issues.length === 0 && <p>현재 필터에 해당하는 QA 오류가 없습니다.</p>}
        {issues.map((issue) => (
          <button key={issue.id} type="button" className={`qa-issue-card ${issue.severity} ${selectedIssueId === issue.id ? 'current' : ''}`} onClick={() => void navigateIssue(issue)}>
            <span>{issue.severity === 'error' ? <AlertCircle size={14} /> : <CheckCircle2 size={14} />}<strong>{SEVERITY_LABELS[issue.severity]}</strong><code>{issue.rule_id}</code></span>
            <p>{issue.message}</p>
            <small>{issue.feature_id ? `피처 ${issue.feature_id}` : '레이어 전체'}{issue.frame_id ? ` · 프레임 ${issue.frame_id}` : ''}</small>
          </button>
        ))}
        {nextOffset !== null && (
          <button
            type="button"
            className="button secondary compact qa-load-more"
            disabled={loading}
            onClick={() => void loadIssues(nextOffset, true)}
          >
            다음 200개 불러오기
          </button>
        )}
      </div>
      {selectedIssue && (
        <footer>
          {selectedIssue.severity === 'error' && (
            <p className="qa-error-resolution-note">
              오류는 데이터를 수정한 뒤 QA 검사를 다시 실행하면 자동으로 해소됩니다.
            </p>
          )}
          {selectedIssue.severity !== 'error' && selectedIssue.status === 'open' && (
            <input aria-label="QA 무시 사유" placeholder="무시 사유 (3자 이상)" value={overrideReason} onChange={(event) => setOverrideReason(event.target.value)} />
          )}
          <div>
            {selectedIssue.severity !== 'error' && selectedIssue.status !== 'open' && <button type="button" className="button secondary compact" disabled={updating} onClick={() => void patchIssue('open')}>다시 열기</button>}
            {selectedIssue.severity !== 'error' && selectedIssue.status === 'open' && <button type="button" className="button primary compact" disabled={updating} onClick={() => void patchIssue('resolved')}>해결 처리</button>}
            {selectedIssue.status === 'open' && selectedIssue.severity !== 'error' && <button type="button" className="button secondary compact" disabled={updating} onClick={() => void patchIssue('dismissed')}>사유로 무시</button>}
          </div>
        </footer>
      )}
    </aside>
  )
}
