import { Download, FileArchive, FolderOpen, Import, LoaderCircle, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, buildApiUrl } from '../lib/api'
import type { RunRecord, RunResults } from '../types'
import './RunResultsDialog.css'

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 1_024) return `${Math.max(0, value || 0)} B`
  const units = ['KB', 'MB', 'GB']
  let size = value / 1_024
  let unit = units[0]
  for (let index = 1; index < units.length && size >= 1_024; index += 1) {
    size /= 1_024
    unit = units[index]
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`
}

export function RunResultsDialog({ run, onClose }: { run: RunRecord | null; onClose: () => void }) {
  const [results, setResults] = useState<RunResults | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [importingPath, setImportingPath] = useState<string | null>(null)
  const [importedPath, setImportedPath] = useState<string | null>(null)

  useEffect(() => {
    if (!run) {
      setResults(null)
      return
    }
    const controller = new AbortController()
    setImportedPath(null)
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
            </div>

            <section className="result-shapefiles">
              <h3><FileArchive size={16} /> SHP 결과</h3>
              {(results.shapefiles ?? []).map((shapefile) => (
                <article key={shapefile.path}>
                  <span>
                    <strong>{shapefile.name}</strong>
                    <small>{shapefile.path}</small>
                  </span>
                  <a className="button secondary" href={buildApiUrl(shapefile.download_url)} download>
                    <Download size={14} /> ZIP 받기
                  </a>
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
            </section>

            <section className="result-file-list">
              <h3>전체 산출물 <small>{results.file_count.toLocaleString('ko-KR')}개</small></h3>
              <div>
                {results.files.map((file) => (
                  <a href={buildApiUrl(file.url)} key={file.path} download>
                    <span><strong>{file.name}</strong><small>{file.path}</small></span>
                    <em>{formatBytes(file.size)}</em>
                    <Download size={14} />
                  </a>
                ))}
              </div>
              {results.truncated && <p>파일 수가 많아 일부만 표시했습니다.</p>}
            </section>
          </div>
        )}
      </section>
    </div>
  )
}
