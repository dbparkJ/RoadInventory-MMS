import { AlertCircle, CheckCircle2, LoaderCircle, Play, RefreshCcw, ShieldCheck, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { hasOpenModalDialog, isWorkspaceShortcutBlockedTarget } from '../lib/frameNavigation'
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
  const requestControllerRef = useRef<AbortController | null>(null)
  const runRequestRef = useRef(0)
  const runControllerRef = useRef<AbortController | null>(null)
  const patchRequestRef = useRef(0)
  const patchControllerRef = useRef<AbortController | null>(null)
  const latestLoadIssuesRef = useRef<((offset?: number, append?: boolean) => Promise<void>) | null>(null)
  const sessionId = review?.session?.id ?? ''
  const activeSessionIdRef = useRef(sessionId)
  activeSessionIdRef.current = sessionId
  const selectedIssue = issues.find((issue) => issue.id === selectedIssueId) ?? null
  const enabled = Boolean(review?.enabled && review.session)

  const loadIssues = useCallback(async (offset = 0, append = false) => {
    if (!sessionId) return
    const requestId = requestRef.current + 1
    requestRef.current = requestId
    requestControllerRef.current?.abort()
    const controller = new AbortController()
    requestControllerRef.current = controller
    const isCurrentRequest = () => (
      !controller.signal.aborted &&
      requestRef.current === requestId &&
      activeSessionIdRef.current === sessionId
    )
    setLoading(true)
    setError('')
    if (!append) setNextOffset(null)
    try {
      const response = await api.qaIssues(
        sessionId,
        {
          offset,
          limit: 200,
          status: statusFilter,
          severity: severityFilter || undefined,
        },
        controller.signal,
      )
      if (!isCurrentRequest()) return
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
      if (!isCurrentRequest()) return
      setError(reason instanceof Error ? reason.message : 'QA 오류를 불러오지 못했습니다.')
    } finally {
      if (isCurrentRequest()) setLoading(false)
      if (requestControllerRef.current === controller) requestControllerRef.current = null
    }
  }, [sessionId, severityFilter, statusFilter])
  latestLoadIssuesRef.current = loadIssues

  useEffect(() => {
    requestRef.current += 1
    requestControllerRef.current?.abort()
    requestControllerRef.current = null
    runRequestRef.current += 1
    runControllerRef.current?.abort()
    runControllerRef.current = null
    patchRequestRef.current += 1
    patchControllerRef.current?.abort()
    patchControllerRef.current = null
    setIssues([])
    setTotal(0)
    setNextOffset(null)
    setSelectedIssueId('')
    setOverrideReason('')
    setLoading(false)
    setRunning(false)
    setUpdating(false)
    setError('')
  }, [sessionId])

  useEffect(() => {
    if (!open || !enabled) return
    void loadIssues()
  }, [enabled, loadIssues, open])

  useEffect(() => {
    if (enabled) return
    requestRef.current += 1
    requestControllerRef.current?.abort()
    requestControllerRef.current = null
    runRequestRef.current += 1
    runControllerRef.current?.abort()
    runControllerRef.current = null
    patchRequestRef.current += 1
    patchControllerRef.current?.abort()
    patchControllerRef.current = null
    setOpen(false)
    setIssues([])
    setTotal(0)
    setNextOffset(null)
  }, [enabled])

  useEffect(
    () => () => {
      requestControllerRef.current?.abort()
      runControllerRef.current?.abort()
      patchControllerRef.current?.abort()
    },
    [],
  )

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        hasOpenModalDialog() ||
        event.defaultPrevented ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.code !== 'KeyQ' ||
        isWorkspaceShortcutBlockedTarget(event.target) ||
        !enabled
      ) return
      event.preventDefault()
      setOpen((value) => !value)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [enabled])

  const runQa = async () => {
    if (!review?.session || !sessionId || running) return
    const requestId = runRequestRef.current + 1
    runRequestRef.current = requestId
    runControllerRef.current?.abort()
    const controller = new AbortController()
    runControllerRef.current = controller
    const isCurrentRequest = () => (
      !controller.signal.aborted &&
      runRequestRef.current === requestId &&
      activeSessionIdRef.current === sessionId
    )
    setRunning(true)
    setError('')
    try {
      const result = await api.runQa(sessionId, controller.signal)
      if (!isCurrentRequest()) return
      await review.recordQaRun(result, sessionId)
      if (!isCurrentRequest()) return
      await latestLoadIssuesRef.current?.()
    } catch (reason) {
      if (!isCurrentRequest()) return
      setError(reason instanceof Error ? reason.message : 'QA 검사를 실행하지 못했습니다.')
    } finally {
      if (isCurrentRequest()) setRunning(false)
      if (runControllerRef.current === controller) runControllerRef.current = null
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
    const issue = selectedIssue
    const requestSessionId = sessionId
    const requestId = patchRequestRef.current + 1
    patchRequestRef.current = requestId
    patchControllerRef.current?.abort()
    const controller = new AbortController()
    patchControllerRef.current = controller
    const isCurrentRequest = () => (
      !controller.signal.aborted &&
      patchRequestRef.current === requestId &&
      activeSessionIdRef.current === requestSessionId
    )
    setUpdating(true)
    setError('')
    try {
      await api.patchQaIssue(issue.id, {
        status,
        ...(status === 'dismissed' ? { override_reason: reason } : {}),
      }, controller.signal)
      if (!isCurrentRequest()) return
      await latestLoadIssuesRef.current?.()
    } catch (reason) {
      if (!isCurrentRequest()) return
      setError(reason instanceof Error ? reason.message : 'QA 상태를 저장하지 못했습니다.')
    } finally {
      if (isCurrentRequest()) setUpdating(false)
      if (patchControllerRef.current === controller) patchControllerRef.current = null
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
