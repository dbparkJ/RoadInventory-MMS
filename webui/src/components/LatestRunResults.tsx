import {
  ChevronRight,
  CircleDashed,
  FileCheck2,
  ListChecks,
  LoaderCircle,
  X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError, api } from '../lib/api'
import { formatDate } from '../lib/format'
import type { DatasetSummary, RunRecord } from '../types'
import { RunResultsDialog } from './RunResultsDialog'

const HISTORY_FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function historyFocusableElements(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(HISTORY_FOCUSABLE_SELECTOR))
    .filter((element) => element.getAttribute('aria-hidden') !== 'true')
}

export function completedRunsForDataset(
  runs: RunRecord[],
  datasetId: string,
): RunRecord[] {
  if (!datasetId) return []
  return runs
    .filter((run) => run.dataset_id === datasetId && run.status === 'completed')
    .sort((left, right) => {
      const leftTime = Date.parse(left.finished_at ?? left.updated_at ?? left.created_at)
      const rightTime = Date.parse(right.finished_at ?? right.updated_at ?? right.created_at)
      const completionOrder = (Number.isFinite(rightTime) ? rightTime : 0) -
        (Number.isFinite(leftTime) ? leftTime : 0)
      if (completionOrder !== 0) return completionOrder

      const leftCreated = Date.parse(left.created_at)
      const rightCreated = Date.parse(right.created_at)
      const creationOrder = (Number.isFinite(rightCreated) ? rightCreated : 0) -
        (Number.isFinite(leftCreated) ? leftCreated : 0)
      if (creationOrder !== 0 || left.id === right.id) return creationOrder
      return left.id < right.id ? 1 : -1
    })
}

export function latestCompletedRunForDataset(
  runs: RunRecord[],
  datasetId: string,
): RunRecord | null {
  return completedRunsForDataset(runs, datasetId)[0] ?? null
}

export function LatestRunResults({
  dataset,
  runs,
  demoMode,
  onOpenQueue,
}: {
  dataset: DatasetSummary | null
  runs: RunRecord[]
  demoMode: boolean
  onOpenQueue: () => void
}) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<RunRecord[]>([])
  const [resultRun, setResultRun] = useState<RunRecord | null>(null)
  const [lookupLoading, setLookupLoading] = useState(false)
  const [lookupError, setLookupError] = useState<string | null>(null)
  const lookupControllerRef = useRef<AbortController | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const historyDialogRef = useRef<HTMLElement>(null)
  const historyCloseRef = useRef<HTMLButtonElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(null)
  const historyWasOpenRef = useRef(false)

  const close = () => {
    lookupControllerRef.current?.abort()
    lookupControllerRef.current = null
    setOpen(false)
    setResultRun(null)
    setLookupLoading(false)
    setLookupError(null)
  }

  const openResults = async () => {
    if (!open) {
      restoreFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : triggerRef.current
    }
    lookupControllerRef.current?.abort()
    lookupControllerRef.current = null
    setOpen(true)
    setResultRun(null)
    setItems([])
    setLookupError(null)

    if (!dataset) {
      setLookupLoading(false)
      return
    }
    if (demoMode) {
      setLookupLoading(false)
      setItems(completedRunsForDataset(runs, dataset.id))
      return
    }

    const controller = new AbortController()
    lookupControllerRef.current = controller
    setLookupLoading(true)
    try {
      const pageSize = 200
      const completedItems: RunRecord[] = []
      let offset = 0
      let snapshotAt: string | undefined
      while (true) {
        const response = await api.completedRuns(
          dataset.id,
          controller.signal,
          pageSize,
          offset,
          snapshotAt,
        )
        completedItems.push(...response.items)
        if (controller.signal.aborted || lookupControllerRef.current !== controller) return
        snapshotAt = snapshotAt ?? response.snapshot_at
        const nextOffset = response.next_offset
        if (typeof nextOffset !== 'number' || nextOffset <= offset) break
        offset = nextOffset
      }
      if (!controller.signal.aborted && lookupControllerRef.current === controller) {
        setItems(completedItems)
      }
    } catch (reason) {
      if (controller.signal.aborted || lookupControllerRef.current !== controller) return
      const status = reason instanceof ApiError
        ? reason.status
        : typeof reason === 'object' && reason !== null && 'status' in reason
          ? Number(reason.status)
          : 0
      if (status === 404) {
        try {
          const legacy = await api.runs(controller.signal, 200)
          if (!controller.signal.aborted && lookupControllerRef.current === controller) {
            setItems(completedRunsForDataset([...legacy.items, ...runs], dataset.id))
          }
        } catch (legacyReason) {
          if (!controller.signal.aborted && lookupControllerRef.current === controller) {
            setLookupError(
              legacyReason instanceof Error
                ? legacyReason.message
                : '검출결과 실행 목록을 불러오지 못했습니다.',
            )
          }
        }
      } else {
        setLookupError(
          reason instanceof Error ? reason.message : '검출결과 실행 목록을 불러오지 못했습니다.',
        )
      }
    } finally {
      if (lookupControllerRef.current === controller) {
        lookupControllerRef.current = null
        setLookupLoading(false)
      }
    }
  }

  useEffect(() => {
    lookupControllerRef.current?.abort()
    lookupControllerRef.current = null
    setOpen(false)
    setItems([])
    setResultRun(null)
    setLookupLoading(false)
    setLookupError(null)
  }, [dataset?.id])

  useEffect(() => {
    if (!open || resultRun) return
    const dialog = historyDialogRef.current
    if (!dialog) return
    const ownerDocument = dialog.ownerDocument
    const handleDialogKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        close()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = historyFocusableElements(dialog)
      if (!focusable.length) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = ownerDocument.activeElement
      if (event.shiftKey && (active === first || active === dialog || !dialog.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || active === dialog || !dialog.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }
    ownerDocument.addEventListener('keydown', handleDialogKey, true)
    historyCloseRef.current?.focus()
    return () => ownerDocument.removeEventListener('keydown', handleDialogKey, true)
  }, [open, resultRun])

  useEffect(() => {
    if (open) {
      historyWasOpenRef.current = true
      return
    }
    if (!historyWasOpenRef.current) return
    historyWasOpenRef.current = false
    const restoreTarget = restoreFocusRef.current ?? triggerRef.current
    restoreFocusRef.current = null
    restoreTarget?.focus()
  }, [open])

  useEffect(
    () => () => {
      lookupControllerRef.current?.abort()
    },
    [],
  )

  const listDialog = open && !resultRun
    ? createPortal(
        <div
          className="result-dialog-layer result-history-layer"
          role="presentation"
          onMouseDown={(event) => event.target === event.currentTarget && close()}
        >
          <section
            ref={historyDialogRef}
            className="result-dialog result-history-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="작업별 검출결과"
            tabIndex={-1}
          >
            <header>
              <div>
                <span className="eyebrow">DETECTION HISTORY</span>
                <h2>작업별 검출결과</h2>
                <small>
                  {dataset
                    ? `${dataset.name} · 완료 작업 ${items.length.toLocaleString('ko-KR')}건`
                    : '선택한 작업 데이터가 없습니다'}
                </small>
              </div>
              <button
                ref={historyCloseRef}
                type="button"
                className="icon-button"
                onClick={close}
                aria-label="결과 목록 닫기"
              >
                <X size={18} />
              </button>
            </header>
            <div className="result-history-body">
              {lookupLoading ? (
                <div className="result-loading" role="status">
                  <LoaderCircle className="spin" />
                  <span>완료된 자동 검출 작업을 불러오고 있습니다.</span>
                </div>
              ) : lookupError ? (
                <div className="result-empty-state result-lookup-error" role="alert">
                  <CircleDashed size={34} aria-hidden="true" />
                  <strong>검출결과 목록을 확인하지 못했습니다</strong>
                  <p>{lookupError}</p>
                  <button type="button" className="button secondary" onClick={() => void openResults()}>
                    다시 시도
                  </button>
                </div>
              ) : !dataset || !items.length ? (
                <div className="result-empty-state">
                  <CircleDashed size={34} aria-hidden="true" />
                  <strong>{dataset ? '완료된 자동 검출 작업이 없습니다' : '먼저 작업 데이터를 선택해 주세요'}</strong>
                  <p>자동 검출을 실행하면 작업별 산출물을 이 목록에서 다시 열 수 있습니다.</p>
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => {
                      close()
                      onOpenQueue()
                    }}
                  >
                    <ListChecks size={15} /> 실행 큐 확인
                  </button>
                </div>
              ) : (
                <div className="result-history-list">
                  {items.map((run, index) => {
                    const range = run.request?.frame_range
                    const trackCount = run.request?.track_ids.length
                    return (
                      <button
                        type="button"
                        className="result-history-card"
                        key={run.id}
                        onClick={() => setResultRun(run)}
                      >
                        <span className="result-history-index">{String(index + 1).padStart(2, '0')}</span>
                        <span className="result-history-copy">
                          <strong>{formatDate(run.finished_at ?? run.updated_at ?? run.created_at)}</strong>
                          <small>
                            {trackCount !== undefined ? `${trackCount}개 트랙` : '트랙 정보 없음'}
                            {' · '}
                            {range ? `Frame ${range[0] + 1}–${range[1] + 1}` : '전체 프레임'}
                          </small>
                          <code>{run.id}</code>
                        </span>
                        <span className="result-history-open">
                          상세 보기 <ChevronRight size={15} />
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
          </section>
        </div>,
        document.body,
      )
    : null

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="queue-button detection-results-button"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-busy={lookupLoading}
        onClick={() => void openResults()}
        title={dataset ? `${dataset.name}의 작업별 검출결과 확인` : '작업 데이터를 먼저 선택하세요'}
      >
        {lookupLoading ? <LoaderCircle size={16} className="spin" /> : <FileCheck2 size={16} />}
        검출결과
      </button>
      {listDialog}
      <RunResultsDialog
        run={resultRun}
        contextLabel="선택한 자동 검출 작업"
        onClose={() => setResultRun(null)}
      />
    </>
  )
}
