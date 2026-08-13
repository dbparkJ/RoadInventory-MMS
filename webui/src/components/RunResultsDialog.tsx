import { ChevronDown, Download, FileArchive, FolderOpen, Images, Import, LoaderCircle, PackageOpen, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { api, buildApiUrl } from '../lib/api'
import type { RunRecord, RunResults } from '../types'
import './RunResultsDialog.css'

export function RunResultsDialog({ run, onClose }: { run: RunRecord | null; onClose: () => void }) {
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

  if (!run) return null

  const importShapefile = async (path: string, name: string) => {
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

  return (
    <div className="result-dialog-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="result-dialog" role="dialog" aria-modal="true" aria-label="검출 결과">
        <header>
          <div>
            <span className="eyebrow">DETECTION OUTPUT</span>
            <h2>검출 결과</h2>
            <small>{run.dataset_name ?? run.dataset_id} · {run.id}</small>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="결과 닫기"><X size={18} /></button>
        </header>
        {loading && <div className="result-loading"><LoaderCircle className="spin" /><span>서버 결과 목록을 확인하고 있습니다.</span></div>}
        {error && <div className="result-error">{error}</div>}
        {results && (
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
                      <strong>{shapefile.name}</strong>
                      <small>{shapefile.path}</small>
                    </span>
                    <button
                      type="button"
                      className="button primary"
                      disabled={importingPath !== null || importedPath === shapefile.path}
                      onClick={() => void importShapefile(shapefile.path, shapefile.name)}
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
}
