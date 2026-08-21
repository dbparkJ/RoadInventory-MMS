import { ChevronDown, CircleDashed, Download, FileArchive, FolderOpen, Images, Import, ListChecks, LoaderCircle, PackageOpen, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, buildApiUrl } from '../lib/api'
import type { RunRecord, RunResults } from '../types'
import './RunResultsDialog.css'

interface ResultEmptyState {
  open: boolean
  datasetName?: string
  loading?: boolean
  error?: string | null
  onOpenQueue?: () => void
  onRetry?: () => void
}

export function RunResultsDialog({
  run,
  onClose,
  contextLabel,
  emptyState,
}: {
  run: RunRecord | null
  onClose: () => void
  contextLabel?: string
  emptyState?: ResultEmptyState
}) {
  const [results, setResults] = useState<RunResults | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [importingPath, setImportingPath] = useState<string | null>(null)
  const [importedPath, setImportedPath] = useState<string | null>(null)
  const [archiveMenuOpen, setArchiveMenuOpen] = useState(false)
  const archiveMenuId = useId()
  const archiveMenuRef = useRef<HTMLDivElement>(null)
  const archiveTriggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!run) {
      setResults(null)
      setLoading(false)
      setError(null)
      return
    }
    const controller = new AbortController()
    setImportedPath(null)
    setArchiveMenuOpen(false)
    setLoading(true)
    setError(null)
    void api
      .runResults(run.id, controller.signal)
      .then(setResults)
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : '결과 목록을 불러오지 못했습니다.')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [run])

  useEffect(() => {
    if (!archiveMenuOpen) return
    const ownerDocument = archiveMenuRef.current?.ownerDocument ?? document
    const closeForOutsidePointer = (event: PointerEvent) => {
      if (!archiveMenuRef.current?.contains(event.target as Node)) setArchiveMenuOpen(false)
    }
    const closeForEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setArchiveMenuOpen(false)
      archiveTriggerRef.current?.focus()
    }
    ownerDocument.addEventListener('pointerdown', closeForOutsidePointer)
    ownerDocument.addEventListener('keydown', closeForEscape)
    archiveMenuRef.current?.querySelector<HTMLAnchorElement>('[role="menuitem"]')?.focus()
    return () => {
      ownerDocument.removeEventListener('pointerdown', closeForOutsidePointer)
      ownerDocument.removeEventListener('keydown', closeForEscape)
    }
  }, [archiveMenuOpen])

  if (!run && !emptyState?.open) return null

  const importShapefile = async (path: string, name: string) => {
    if (!run) return
    setImportingPath(path)
    setError(null)
    try {
      const response = await api.importRunShapefile(run.id, path, name)
      setImportedPath(path)
      window.dispatchEvent(
        new CustomEvent('mms-overlay-changed', {
          detail: { open: true, datasetId: run.dataset_id, layerId: response.layer.id },
        }),
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'SHP 검수 레이어를 열지 못했습니다.')
    } finally {
      setImportingPath(null)
    }
  }

  const dialog = (
    <div className="result-dialog-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="result-dialog" role="dialog" aria-modal="true" aria-label="검출 결과">
        <header>
          <div>
            <span className="eyebrow">DETECTION OUTPUT</span>
            <h2>검출 결과</h2>
            <small>
              {run
                ? `${contextLabel ? `${contextLabel} · ` : ''}${run.dataset_name ?? run.dataset_id} · ${run.id}`
                : emptyState?.datasetName
                  ? `${emptyState.datasetName} · 최신 완료 실행`
                  : '선택한 작업 데이터 · 최신 완료 실행'}
            </small>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="결과 닫기"><X size={18} /></button>
        </header>
        {!run && emptyState?.open && (
          emptyState.loading ? (
            <div className="result-loading" role="status">
              <LoaderCircle className="spin" />
              <span>최신 완료 실행을 확인하고 있습니다.</span>
            </div>
          ) : emptyState.error ? (
            <div className="result-empty-state result-lookup-error" role="alert">
              <CircleDashed size={34} aria-hidden="true" />
              <strong>최신 검출결과를 확인하지 못했습니다</strong>
              <p>{emptyState.error}</p>
              {emptyState.onRetry && (
                <button type="button" className="button secondary" onClick={emptyState.onRetry}>
                  다시 시도
                </button>
              )}
            </div>
          ) : (
            <div className="result-empty-state">
              <CircleDashed size={34} aria-hidden="true" />
              <strong>
                {emptyState.datasetName
                  ? '완료된 자동 검출결과가 없습니다'
                  : '먼저 작업 데이터를 선택해 주세요'}
              </strong>
              <p>
                {emptyState.datasetName
                  ? `${emptyState.datasetName}에서 완료된 실행이 생기면 이곳에서 최신 결과를 바로 확인할 수 있습니다.`
                  : '작업 데이터 패널에서 데이터를 선택하면 해당 데이터의 최신 완료 결과를 표시합니다.'}
              </p>
              {emptyState.onOpenQueue && (
                <button type="button" className="button secondary" onClick={emptyState.onOpenQueue}>
                  <ListChecks size={15} />
                  실행 큐 확인
                </button>
              )}
            </div>
          )
        )}
        {loading && <div className="result-loading"><LoaderCircle className="spin" /><span>서버 결과 목록을 확인하고 있습니다.</span></div>}
        {error && <div className="result-error">{error}</div>}
        {results && run && (
          <div className="result-dialog-body">
            <div className="result-location">
              <FolderOpen size={17} />
              <span>
                <strong>서버 결과 위치</strong>
                <code>{results.output_location?.relative_path ?? `runs/${run.id}/output`}</code>
              </span>
              <div className="result-archive-menu" ref={archiveMenuRef}>
                <button
                  ref={archiveTriggerRef}
                  type="button"
                  className="button primary result-archive-trigger"
                  aria-haspopup="menu"
                  aria-expanded={archiveMenuOpen}
                  aria-controls={archiveMenuOpen ? archiveMenuId : undefined}
                  disabled={!results.archives}
                  title={results.archives ? '받을 ZIP 종류 선택' : '압축 다운로드를 준비할 수 없습니다.'}
                  onClick={() => setArchiveMenuOpen((open) => !open)}
                >
                  <Download size={14} /> ZIP 받기 <ChevronDown size={13} />
                </button>
                {archiveMenuOpen && results.archives && (
                  <div id={archiveMenuId} className="result-archive-options" role="menu" aria-label="ZIP 종류 선택">
                    <a
                      role="menuitem"
                      href={buildApiUrl(results.archives.all.url)}
                      download={results.archives.all.filename}
                      onClick={() => setArchiveMenuOpen(false)}
                    >
                      <PackageOpen size={17} />
                      <span><strong>전체 산출물</strong><small>공개 가능한 산출물을 폴더 구조 그대로 받습니다.</small></span>
                    </a>
                    <a
                      role="menuitem"
                      href={buildApiUrl(results.archives.detected_images.url)}
                      download={results.archives.detected_images.filename}
                      onClick={() => setArchiveMenuOpen(false)}
                    >
                      <Images size={17} />
                      <span><strong>검출된 사진</strong><small>검출 결과의 image_crops 이미지만 받습니다.</small></span>
                    </a>
                  </div>
                )}
              </div>
            </div>

            <section className="result-shapefiles">
              <h3><FileArchive size={16} /> SHP 결과</h3>
              <div className="result-shapefile-list">
                {(results.shapefiles ?? []).map((shapefile) => (
                  <article key={shapefile.path}>
                    <span>
                      <strong>{shapefile.display_name ?? shapefile.name}</strong>
                      <small>{shapefile.path}</small>
                    </span>
                    <button
                      type="button"
                      className="button primary"
                      disabled={importingPath !== null || importedPath === shapefile.path}
                      onClick={() => void importShapefile(
                        shapefile.path,
                        shapefile.display_name ?? shapefile.name,
                      )}
                    >
                      {importingPath === shapefile.path ? <LoaderCircle size={14} className="spin" /> : <Import size={14} />}
                      {importedPath === shapefile.path ? '검수 레이어에 추가됨' : '검수 레이어로 열기'}
                    </button>
                  </article>
                ))}
                {!results.shapefiles?.length && <p>완성된 SHP 묶음이 없습니다.</p>}
              </div>
            </section>
          </div>
        )}
      </section>
    </div>
  )

  // This dialog can be opened by a button inside the blurred top bar. A fixed
  // descendant of a backdrop-filter element uses that element as its containing
  // block in Chromium, which clipped the dialog above the viewport. Mounting at
  // the document body keeps it viewport-bound regardless of the trigger location.
  return createPortal(dialog, document.body)
}
