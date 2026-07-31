import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  CloudUpload,
  Database,
  File,
  Folder,
  FolderOpen,
  HardDrive,
  LoaderCircle,
  RefreshCcw,
  Server,
  Upload,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import { formatBytes, joinPath } from '../lib/format'
import { uploadFileWithResume, uploadManifestKey } from '../lib/upload'
import type { DatasetDetail, StorageEntry, StorageRoot, UploadSession } from '../types'

type SourceTab = 'server' | 'upload'
const reusableUploadSessions = new Map<string, UploadSession>()

export function StorageDialog({
  open,
  demoMode,
  onClose,
  onDatasetReady,
  onUseDemo,
}: {
  open: boolean
  demoMode: boolean
  onClose: () => void
  onDatasetReady: (dataset: DatasetDetail) => void
  onUseDemo: () => void
}) {
  const [tab, setTab] = useState<SourceTab>('server')
  const [roots, setRoots] = useState<StorageRoot[]>([])
  const [rootId, setRootId] = useState('')
  const [path, setPath] = useState('')
  const [entries, setEntries] = useState<StorageEntry[]>([])
  const [treeTruncated, setTreeTruncated] = useState(false)
  const [treeLoading, setTreeLoading] = useState(false)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [crs, setCrs] = useState('')
  const [scanning, setScanning] = useState(false)
  const [scanStage, setScanStage] = useState('')

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setStorageError(null)
    void api
      .storage(controller.signal)
      .then((response) => {
        const availableRoots = Array.isArray(response) ? (response as unknown as StorageRoot[]) : response.roots
        setRoots(availableRoots)
        setRootId((current) => current || availableRoots[0]?.id || '')
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setStorageError(reason instanceof Error ? reason.message : '서버 저장소를 불러오지 못했습니다.')
        }
      })
    return () => controller.abort()
  }, [open])

  useEffect(() => {
    if (!open || !rootId || tab !== 'server') return
    const controller = new AbortController()
    setTreeLoading(true)
    setTreeTruncated(false)
    setStorageError(null)
    void api
      .storageTree(rootId, path, controller.signal)
      .then((response) => {
        setEntries(response.entries)
        setTreeTruncated(Boolean(response.truncated))
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setStorageError(reason instanceof Error ? reason.message : '폴더 내용을 불러오지 못했습니다.')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setTreeLoading(false)
      })
    return () => controller.abort()
  }, [open, path, rootId, tab])

  const scan = async (targetRootId = rootId, targetPath = path) => {
    if (!targetRootId) return
    setScanning(true)
    setScanStage('폴더 구조와 센서 파일을 확인하는 중')
    try {
      let dataset = await api.scanDataset(targetRootId, targetPath, crs.trim())
      if (dataset.status === 'indexing') {
        const started = Date.now()
        setScanStage('프레임 위치와 주행 경로를 준비하는 중')
        while (dataset.status === 'indexing' && Date.now() - started < 120_000) {
          await new Promise((resolve) => window.setTimeout(resolve, 1_500))
          dataset = await api.dataset(dataset.id)
        }
      }
      if (dataset.status === 'error') throw new Error('데이터셋 인덱싱에 실패했습니다.')
      if (dataset.status !== 'ready') throw new Error('인덱싱 시간이 초과되었습니다. 잠시 후 다시 선택해 주세요.')
      setScanStage('작업 공간을 준비했습니다')
      onDatasetReady(dataset)
      onClose()
    } catch (reason) {
      setStorageError(reason instanceof Error ? reason.message : '데이터셋을 준비하지 못했습니다.')
    } finally {
      setScanning(false)
    }
  }

  if (!open) return null

  return (
    <div className="modal-layer" role="presentation">
      <button type="button" className="modal-scrim" aria-label="닫기" onClick={onClose} />
      <section className="source-dialog" role="dialog" aria-modal="true" aria-labelledby="source-title">
        <header className="dialog-header">
          <div>
            <span className="dialog-icon">
              <Database size={19} />
            </span>
            <div>
              <span className="eyebrow">DATA SOURCE</span>
              <h2 id="source-title">작업 데이터 연결</h2>
            </div>
          </div>
          <button type="button" className="icon-button" onClick={onClose}>
            <X size={18} />
          </button>
        </header>

        <div className="source-tabs">
          <button type="button" className={tab === 'server' ? 'active' : ''} onClick={() => setTab('server')}>
            <Server size={16} />
            서버 폴더
          </button>
          <button type="button" className={tab === 'upload' ? 'active' : ''} onClick={() => setTab('upload')}>
            <CloudUpload size={16} />
            폴더 업로드
          </button>
        </div>

        {tab === 'server' ? (
          <ServerBrowser
            roots={roots}
            rootId={rootId}
            path={path}
            entries={entries}
            loading={treeLoading}
            truncated={treeTruncated}
            error={storageError}
            crs={crs}
            scanning={scanning}
            scanStage={scanStage}
            onRootChange={(id) => {
              setRootId(id)
              setPath('')
            }}
            onPathChange={setPath}
            onCrsChange={setCrs}
            onScan={() => void scan()}
            onRetry={() => {
              const current = rootId
              setRootId('')
              window.setTimeout(() => setRootId(current), 0)
            }}
          />
        ) : (
          <FolderUploader
            crs={crs}
            demoMode={demoMode}
            onCrsChange={setCrs}
            onComplete={(result) => void scan(result.root_id, result.relative_path)}
          />
        )}

        {storageError && tab === 'server' && !roots.length && (
          <div className="demo-fallback">
            <div>
              <strong>서버 연결 전에도 화면을 확인할 수 있어요</strong>
              <p>서울 도심 샘플로 지도·파노라마·3D 흐름을 체험합니다.</p>
            </div>
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                onUseDemo()
                onClose()
              }}
            >
              데모 열기
            </button>
          </div>
        )}
      </section>
    </div>
  )
}

function ServerBrowser({
  roots,
  rootId,
  path,
  entries,
  loading,
  truncated,
  error,
  crs,
  scanning,
  scanStage,
  onRootChange,
  onPathChange,
  onCrsChange,
  onScan,
  onRetry,
}: {
  roots: StorageRoot[]
  rootId: string
  path: string
  entries: StorageEntry[]
  loading: boolean
  truncated: boolean
  error: string | null
  crs: string
  scanning: boolean
  scanStage: string
  onRootChange: (id: string) => void
  onPathChange: (path: string) => void
  onCrsChange: (crs: string) => void
  onScan: () => void
  onRetry: () => void
}) {
  const crumbs = path.split('/').filter(Boolean)
  const currentRoot = roots.find((root) => root.id === rootId)
  const directories = entries.filter((entry) => entry.type === 'directory')
  const visibleFiles = entries.filter((entry) => entry.type === 'file').slice(0, 5)

  return (
    <div className="source-content">
      <div className="storage-toolbar">
        <label className="select-shell">
          <HardDrive size={15} />
          <select value={rootId} onChange={(event) => onRootChange(event.target.value)}>
            {!roots.length && <option value="">저장소 연결 중…</option>}
            {roots.map((root) => (
              <option value={root.id} key={root.id}>
                {root.name}
              </option>
            ))}
          </select>
          <ChevronRight size={14} />
        </label>
        {currentRoot?.free_bytes !== undefined && (
          <span className="storage-free">{formatBytes(currentRoot.free_bytes)} 여유</span>
        )}
      </div>

      <nav className="breadcrumbs" aria-label="현재 폴더">
        <button type="button" onClick={() => onPathChange('')}>
          <HardDrive size={13} />
          {currentRoot?.name ?? 'root'}
        </button>
        {crumbs.map((crumb, index) => (
          <span key={`${crumb}-${index}`}>
            <ChevronRight size={12} />
            <button type="button" onClick={() => onPathChange(crumbs.slice(0, index + 1).join('/'))}>
              {crumb}
            </button>
          </span>
        ))}
      </nav>

      <div className="folder-browser" aria-busy={loading}>
        {path && (
          <button
            type="button"
            className="file-row folder"
            onClick={() => onPathChange(crumbs.slice(0, -1).join('/'))}
          >
            <span className="file-icon">
              <ArrowLeft size={17} />
            </span>
            <span>
              <strong>상위 폴더</strong>
              <small>한 단계 위로 이동</small>
            </span>
          </button>
        )}
        {loading
          ? Array.from({ length: 5 }, (_, index) => <div className="file-row-skeleton" key={index} />)
          : directories.map((entry) => (
              <button
                type="button"
                className="file-row folder"
                key={entry.relative_path}
                onClick={() => onPathChange(entry.relative_path)}
              >
                <span className="file-icon">
                  {entry.dataset_hint ? <FolderOpen size={18} /> : <Folder size={18} />}
                </span>
                <span>
                  <strong>{entry.name}</strong>
                  <small>
                    {entry.dataset_hint ? 'MMS 데이터 후보' : '폴더'}
                    {entry.modified_at ? ` · ${entry.modified_at.slice(0, 10)}` : ''}
                  </small>
                </span>
                {entry.dataset_hint && <em className="candidate-tag">READY</em>}
                <ChevronRight size={15} />
              </button>
            ))}
        {!loading &&
          visibleFiles.map((entry) => (
            <div className="file-row file" key={entry.relative_path}>
              <span className="file-icon">
                <File size={17} />
              </span>
              <span>
                <strong>{entry.name}</strong>
                <small>{formatBytes(entry.size_bytes)}</small>
              </span>
            </div>
          ))}
        {!loading && !entries.length && !error && (
          <div className="browser-empty">
            <Folder size={25} />
            <span>빈 폴더입니다.</span>
          </div>
        )}
        {!loading && truncated && !error && (
          <div className="browser-truncated" role="status">
            <AlertTriangle size={14} />
            항목이 많아 서버가 첫 1,000개만 표시했습니다. 더 구체적인 상위 폴더를 선택해 주세요.
          </div>
        )}
        {error && (
          <div className="browser-empty error">
            <AlertTriangle size={24} />
            <span>{error}</span>
            <button type="button" className="text-action" onClick={onRetry}>
              <RefreshCcw size={13} />
              다시 시도
            </button>
          </div>
        )}
        {scanning && (
          <div className="scan-overlay">
            <span className="scan-radar">
              <i />
            </span>
            <strong>{scanStage}</strong>
            <small>대용량 파일은 서버에서 비동기로 준비합니다.</small>
          </div>
        )}
      </div>

      <CrsField value={crs} onChange={onCrsChange} />
      <footer className="source-footer">
        <div>
          <FolderOpen size={16} />
          <span>
            <small>선택한 작업 폴더</small>
            <strong>{path || '/'}</strong>
          </span>
        </div>
        <button
          type="button"
          className="button primary"
          onClick={onScan}
          disabled={!rootId || scanning}
        >
          {scanning ? <LoaderCircle size={15} className="spin" /> : <Database size={15} />}
          {scanning ? '데이터 준비 중' : '이 폴더로 작업'}
        </button>
      </footer>
    </div>
  )
}

function CrsField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <label className="crs-field">
      <span>
        <strong>원본 좌표계 (CRS)</strong>
        <small>LAS와 pose 좌표계가 반드시 같아야 합니다.</small>
      </span>
      <input
        list="mms-crs-options"
        value={value}
        placeholder="비우면 서버가 감지"
        onChange={(event) => onChange(event.target.value)}
      />
      <datalist id="mms-crs-options">
        <option value="EPSG:32652">WGS 84 / UTM zone 52N</option>
        <option value="EPSG:5179">Korea 2000 / Unified CS</option>
        <option value="EPSG:5186">Korea 2000 / Central Belt</option>
      </datalist>
    </label>
  )
}

interface UploadFile {
  file: File
  path: string
}

function sessionMatchesFiles(session: UploadSession, files: UploadFile[]): boolean {
  if (session.files.length !== files.length) return false
  const remoteByPath = new Map(session.files.map((item) => [item.path, item]))
  return files.every(({ file, path }) => {
    const remote = remoteByPath.get(path)
    return Boolean(remote && (remote.size === undefined || remote.size === file.size))
  })
}

function FolderUploader({
  crs,
  demoMode,
  onCrsChange,
  onComplete,
}: {
  crs: string
  demoMode: boolean
  onCrsChange: (value: string) => void
  onComplete: (result: { root_id: string; relative_path: string }) => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const [files, setFiles] = useState<UploadFile[]>([])
  const [progress, setProgress] = useState(0)
  const [uploadedBytes, setUploadedBytes] = useState(0)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    inputRef.current?.setAttribute('webkitdirectory', '')
    inputRef.current?.setAttribute('directory', '')
  }, [])

  const totalBytes = useMemo(() => files.reduce((sum, item) => sum + item.file.size, 0), [files])
  const folderName = files[0]?.path.split('/')[0]
  const uploadKey = useMemo(
    () =>
      files.length
        ? uploadManifestKey(
            files.map(({ file, path }) => ({
              path,
              size: file.size,
              lastModified: file.lastModified,
            })),
          )
        : '',
    [files],
  )
  const canResume = Boolean(uploadKey && reusableUploadSessions.has(uploadKey))

  const select = (selected: FileList | File[], requireRelativePaths = false) => {
    const chosen = Array.from(selected)
    const relativePaths = chosen.map((file) =>
      (file as File & { webkitRelativePath?: string }).webkitRelativePath?.replaceAll('\\', '/'),
    )
    if (requireRelativePaths && relativePaths.some((relativePath) => !relativePath)) {
      setFiles([])
      setProgress(0)
      setUploadedBytes(0)
      setError(
        '이 브라우저는 드롭한 폴더의 상대 경로를 제공하지 않습니다. 폴더 구조 보존을 위해 클릭하여 선택해 주세요.',
      )
      return
    }
    const next = chosen.map((file, index) => ({
      file,
      path: relativePaths[index] || file.name,
    }))
    setFiles(next)
    setProgress(0)
    setUploadedBytes(0)
    setError(null)
  }

  const upload = async () => {
    if (!files.length) return
    const controller = new AbortController()
    abortRef.current = controller
    setUploading(true)
    setError(null)
    try {
      const manifest = files.map(({ file, path }) => ({
        path,
        size: file.size,
        type: file.type || 'application/octet-stream',
        last_modified: file.lastModified,
      }))
      let session = reusableUploadSessions.get(uploadKey)
      let reused = Boolean(session)
      if (session && !sessionMatchesFiles(session, files)) {
        reusableUploadSessions.delete(uploadKey)
        session = undefined
        reused = false
      }
      if (session) {
        const firstRemote = session.files[0]
        try {
          firstRemote.uploaded_bytes = await api.uploadedBytes(session.id, firstRemote.id)
        } catch {
          reusableUploadSessions.delete(uploadKey)
          session = undefined
          reused = false
        }
      }
      if (!session) {
        session = await api.createUpload(
          folderName ?? `mms-upload-${new Date().toISOString().slice(0, 10)}`,
          manifest,
        )
        reusableUploadSessions.set(uploadKey, session)
      }
      const activeSession = session

      const remoteByPath = new Map(activeSession.files.map((item) => [item.path, item]))
      const pending = files.map((item) => ({ item, remote: remoteByPath.get(item.path) }))
      if (pending.some(({ remote }) => !remote)) {
        reusableUploadSessions.delete(uploadKey)
        throw new Error('서버 업로드 목록과 선택한 폴더가 일치하지 않습니다. 새로 선택해 주세요.')
      }

      let completedBytes = pending.reduce(
        (sum, { remote }) => sum + Number(remote?.uploaded_bytes ?? 0),
        0,
      )
      const updateProgress = () => {
        setUploadedBytes(completedBytes)
        setProgress(totalBytes > 0 ? Math.round((completedBytes / totalBytes) * 100) : 0)
      }
      updateProgress()

      const workers = Array.from({ length: Math.min(3, pending.length) }, async () => {
        while (pending.length) {
          const next = pending.shift()
          if (!next) break
          const { item, remote } = next
          if (!remote) throw new Error(`${item.path} 업로드 슬롯을 받지 못했습니다.`)
          await uploadFileWithResume({
            file: item.file,
            chunkSize: activeSession.chunk_size,
            initialOffset: remote.uploaded_bytes ?? 0,
            confirmInitialOffset: reused,
            signal: controller.signal,
            maxRetries: 3,
            getUploadedBytes: () => api.uploadedBytes(activeSession.id, remote.id),
            putChunk: (chunk, start, total) =>
              api.uploadChunk(
                activeSession.id,
                remote.id,
                item.path,
                chunk,
                start,
                total,
                controller.signal,
              ),
            onOffsetChange: (nextOffset, previousOffset) => {
              remote.uploaded_bytes = nextOffset
              completedBytes += nextOffset - previousOffset
              updateProgress()
            },
          })
        }
      })
      await Promise.all(workers)
      const result = await api.completeUpload(activeSession.id)
      reusableUploadSessions.delete(uploadKey)
      setProgress(100)
      setUploadedBytes(totalBytes)
      onComplete(result)
    } catch (reason) {
      const cancelled =
        controller.signal.aborted ||
        (reason instanceof DOMException && reason.name === 'AbortError')
      if (!controller.signal.aborted) controller.abort()
      if (cancelled) {
        setError('전송을 중단했습니다. 이 브라우저 탭에서 같은 파일로 다시 시작하면 확인된 오프셋부터 이어집니다.')
      } else {
        setError(
          `${
            reason instanceof Error ? reason.message : '업로드를 완료하지 못했습니다.'
          } 같은 파일로 다시 시작하면 서버 오프셋을 확인해 이어서 시도합니다.`,
        )
      }
    } finally {
      setUploading(false)
      abortRef.current = null
    }
  }

  return (
    <div className="source-content uploader-content">
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => event.target.files && select(event.target.files)}
      />
      <button
        type="button"
        className={`drop-zone ${files.length ? 'has-files' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault()
          if (event.dataTransfer.files.length) select(event.dataTransfer.files, true)
        }}
      >
        {files.length ? (
          <>
            <span className="drop-icon ready">
              <CheckCircle2 size={24} />
            </span>
            <strong>{folderName ?? '선택한 파일'}</strong>
            <p>
              {files.length.toLocaleString('ko-KR')}개 파일 · {formatBytes(totalBytes)}
            </p>
            <small>클릭하여 다른 폴더 선택</small>
          </>
        ) : (
          <>
            <span className="drop-icon">
              <Upload size={24} />
            </span>
            <strong>MMS 폴더를 선택해 주세요</strong>
            <p>클릭 선택은 상대 폴더 구조를 유지해 분할 업로드합니다.</p>
            <small>드롭은 브라우저가 폴더 경로를 제공할 때만 허용</small>
          </>
        )}
      </button>

      {files.length > 0 && (
        <div className="upload-manifest">
          {files.slice(0, 4).map((item) => (
            <div key={item.path}>
              <File size={14} />
              <span>{item.path}</span>
              <small>{formatBytes(item.file.size)}</small>
            </div>
          ))}
          {files.length > 4 && <em>외 {files.length - 4}개 파일</em>}
        </div>
      )}

      <CrsField value={crs} onChange={onCrsChange} />

      {uploading && (
        <div className="upload-progress">
          <div>
            <span>분할 업로드 중</span>
            <strong>{progress}%</strong>
          </div>
          <div className="progress-track">
            <span style={{ width: `${progress}%` }} />
          </div>
          <small>
            {formatBytes(uploadedBytes)} / {formatBytes(totalBytes)} · 청크 오류는 오프셋 확인 후 최대 3회 재시도
          </small>
        </div>
      )}
      {error && (
        <div className="inline-error">
          <AlertTriangle size={15} />
          {error}
        </div>
      )}
      {demoMode && <p className="upload-hint">데모 모드에서도 실제 서버가 연결되어 있으면 업로드할 수 있습니다.</p>}
      <footer className="source-footer upload-footer">
        <div>
          <CloudUpload size={16} />
          <span>
            <small>전송 방식</small>
            <strong>청크 단위 전송 · 현재 브라우저 탭에서 재시도 지원</strong>
          </span>
        </div>
        {uploading ? (
          <button type="button" className="button danger" onClick={() => abortRef.current?.abort()}>
            <X size={15} />
            전송 중단
          </button>
        ) : (
          <button type="button" className="button primary" disabled={!files.length} onClick={() => void upload()}>
            <Upload size={15} />
            {canResume ? '업로드 이어서' : '업로드 시작'}
          </button>
        )}
      </footer>
    </div>
  )
}
