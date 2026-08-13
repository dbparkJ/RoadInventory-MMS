import { FileCheck2, LoaderCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { DatasetSummary, RunRecord } from '../types'
import { RunResultsDialog } from './RunResultsDialog'

export function latestCompletedRunForDataset(
  runs: RunRecord[],
  datasetId: string,
): RunRecord | null {
  if (!datasetId) return null
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
    })[0] ?? null
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
  const [resultRun, setResultRun] = useState<RunRecord | null>(null)
  const [lookupLoading, setLookupLoading] = useState(false)
  const [lookupError, setLookupError] = useState<string | null>(null)
  const lookupControllerRef = useRef<AbortController | null>(null)

  const close = () => {
    lookupControllerRef.current?.abort()
    lookupControllerRef.current = null
    setOpen(false)
    setLookupLoading(false)
    setLookupError(null)
  }

  const openLatestResults = async () => {
    lookupControllerRef.current?.abort()
    lookupControllerRef.current = null
    setOpen(true)
    setResultRun(null)
    setLookupError(null)

    if (!dataset) {
      setLookupLoading(false)
      return
    }
    if (demoMode) {
      setLookupLoading(false)
      setResultRun(latestCompletedRunForDataset(runs, dataset.id))
      return
    }

    const controller = new AbortController()
    lookupControllerRef.current = controller
    setLookupLoading(true)
    try {
      const response = await api.latestCompletedRun(dataset.id, controller.signal)
      if (!controller.signal.aborted && lookupControllerRef.current === controller) {
        setResultRun(response.run)
      }
    } catch (reason) {
      if (controller.signal.aborted || lookupControllerRef.current !== controller) return
      if (reason instanceof ApiError && reason.status === 404) {
        try {
          // Servers started before latest-completed was introduced expose only
          // the bounded, non-dismissed run collection. Query its largest
          // supported page and combine it with bootstrap state so deployments
          // can be upgraded without showing a raw Not Found error.
          const legacy = await api.runs(controller.signal, 200)
          if (!controller.signal.aborted && lookupControllerRef.current === controller) {
            setResultRun(
              latestCompletedRunForDataset([...legacy.items, ...runs], dataset.id),
            )
          }
        } catch (legacyReason) {
          if (!controller.signal.aborted && lookupControllerRef.current === controller) {
            setLookupError(
              legacyReason instanceof Error
                ? legacyReason.message
                : '최신 완료 실행을 불러오지 못했습니다.',
            )
          }
        }
      } else {
        setLookupError(
          reason instanceof Error ? reason.message : '최신 완료 실행을 불러오지 못했습니다.',
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
    setResultRun(null)
    setLookupLoading(false)
    setLookupError(null)
  }, [dataset?.id])

  useEffect(
    () => () => {
      lookupControllerRef.current?.abort()
    },
    [],
  )

  return (
    <>
      <button
        type="button"
        className="queue-button detection-results-button"
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-busy={lookupLoading}
        onClick={() => void openLatestResults()}
        title={
          dataset
            ? `${dataset.name}의 최신 완료 검출결과 확인`
            : '작업 데이터를 선택한 뒤 최신 검출결과를 확인하세요'
        }
      >
        {lookupLoading ? <LoaderCircle size={16} className="spin" /> : <FileCheck2 size={16} />}
        검출결과
      </button>
      <RunResultsDialog
        run={open ? resultRun : null}
        contextLabel="선택한 작업 데이터의 최신 완료 실행"
        emptyState={{
          open: open && resultRun === null,
          datasetName: dataset?.name,
          loading: lookupLoading,
          error: lookupError,
          onRetry: dataset && !demoMode ? () => void openLatestResults() : undefined,
          onOpenQueue: () => {
            close()
            onOpenQueue()
          },
        }}
        onClose={close}
      />
    </>
  )
}
