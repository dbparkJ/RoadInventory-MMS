import type {
  BootstrapResponse,
  DatasetDetail,
  FrameDetectionResponse,
  FrameLocateResponse,
  FramePage,
  OverlayCoordinateSpace,
  OverlayEncoding,
  OverlayFeature,
  OverlayFeatureCollection,
  OverlayFeatureCreateRequest,
  OverlayFeatureDetail,
  OverlayLayer,
  PanoramaOverlayFeature,
  PanoramaDetectionBoxObservation,
  PanoramaProjectionMetadata,
  RouteResponse,
  RunEvent,
  RunRecord,
  RunRequest,
  RunResults,
  StorageRoot,
  StorageTreeResponse,
  UploadManifestFile,
  UploadSession,
} from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const DEFAULT_TIMEOUT = 15_000

export class ApiError extends Error {
  readonly status: number
  readonly code?: string
  readonly details?: unknown

  constructor(message: string, status = 0, code?: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

interface RequestOptions extends RequestInit {
  timeout?: number
  retries?: number
}

export function buildApiUrl(path: string, query?: Record<string, string | number | undefined>): string {
  if (/^https?:\/\//.test(path) || (API_BASE && path.startsWith(`${API_BASE}/`))) {
    return path
  }
  const normalized = path.startsWith('/') ? path : `/${path}`
  const params = new URLSearchParams()
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== '') params.set(key, String(value))
  })
  const suffix = params.size ? `?${params.toString()}` : ''
  return `${API_BASE}${normalized}${suffix}`
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function mergeSignals(signal: AbortSignal | null | undefined, timeout: number): {
  signal: AbortSignal
  cleanup: () => void
} {
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(new DOMException('Timeout', 'TimeoutError')), timeout)
  const abort = () => controller.abort(signal?.reason)
  signal?.addEventListener('abort', abort, { once: true })
  return {
    signal: controller.signal,
    cleanup: () => {
      window.clearTimeout(timeoutId)
      signal?.removeEventListener('abort', abort)
    },
  }
}

export function errorMessageFromPayload(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== 'object') return fallback
  const record = payload as Record<string, unknown>
  if (typeof record.message === 'string' && record.message.trim()) return record.message
  if (typeof record.detail === 'string' && record.detail.trim()) return record.detail
  if (Array.isArray(record.detail)) {
    const messages = record.detail
      .map((entry) => {
        if (!entry || typeof entry !== 'object') return ''
        const issue = entry as Record<string, unknown>
        if (typeof issue.msg !== 'string' || !issue.msg.trim()) return ''
        const location = Array.isArray(issue.loc)
          ? issue.loc
              .filter((part): part is string | number => typeof part === 'string' || typeof part === 'number')
              .filter((part) => part !== 'body')
              .join('.')
          : ''
        return location ? `${location}: ${issue.msg}` : issue.msg
      })
      .filter(Boolean)
    if (messages.length) return messages.join(' · ')
  }
  if (
    record.detail &&
    typeof record.detail === 'object' &&
    typeof (record.detail as Record<string, unknown>).message === 'string'
  ) {
    return (record.detail as Record<string, string>).message
  }
  return fallback
}

async function parseError(response: Response): Promise<ApiError> {
  const fallback = `요청을 처리하지 못했습니다. (${response.status})`
  try {
    const payload = (await response.json()) as unknown
    const code =
      payload && typeof payload === 'object' && typeof (payload as Record<string, unknown>).code === 'string'
        ? ((payload as Record<string, unknown>).code as string)
        : undefined
    return new ApiError(errorMessageFromPayload(payload, fallback), response.status, code, payload)
  } catch {
    return new ApiError(fallback, response.status)
  }
}

async function request(path: string, options: RequestOptions = {}): Promise<Response> {
  const method = options.method?.toUpperCase() ?? 'GET'
  const retries = options.retries ?? (method === 'GET' || method === 'HEAD' ? 2 : 0)
  let latestError: unknown

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const merged = mergeSignals(options.signal, options.timeout ?? DEFAULT_TIMEOUT)
    try {
      const response = await fetch(buildApiUrl(path), {
        ...options,
        headers: {
          Accept: 'application/json',
          ...options.headers,
        },
        signal: merged.signal,
      })
      if (!response.ok) {
        const error = await parseError(response)
        if (response.status >= 500 && attempt < retries) {
          latestError = error
          await sleep(300 * 2 ** attempt)
          continue
        }
        throw error
      }
      return response
    } catch (error) {
      latestError = error
      const canRetry =
        attempt < retries &&
        !(error instanceof ApiError && error.status > 0 && error.status < 500) &&
        !options.signal?.aborted
      if (!canRetry) {
        if (error instanceof ApiError) throw error
        if (error instanceof DOMException && error.name === 'AbortError') throw error
        throw new ApiError(
          error instanceof DOMException && error.name === 'TimeoutError'
            ? '서버 응답 시간이 초과되었습니다.'
            : '서버에 연결할 수 없습니다.',
          0,
          'NETWORK_ERROR',
          error,
        )
      }
      await sleep(300 * 2 ** attempt)
    } finally {
      merged.cleanup()
    }
  }

  throw latestError
}

async function json<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await request(path, options)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

function jsonBody(value: unknown): Pick<RequestInit, 'body' | 'headers'> {
  return {
    body: JSON.stringify(value),
    headers: { 'Content-Type': 'application/json' },
  }
}

export const api = {
  bootstrap(signal?: AbortSignal) {
    return json<BootstrapResponse>('/api/bootstrap', { signal })
  },

  storage(signal?: AbortSignal) {
    return json<{ roots: StorageRoot[] }>('/api/storage', { signal })
  },

  storageTree(rootId: string, relativePath: string, signal?: AbortSignal) {
    return json<StorageTreeResponse>(
      buildApiUrl(`/api/storage/${encodeURIComponent(rootId)}/tree`, { path: relativePath }),
      { signal },
    )
  },

  scanDataset(rootId: string, relativePath: string, crs: string) {
    return json<DatasetDetail>('/api/datasets/scan', {
      method: 'POST',
      ...jsonBody({ root_id: rootId, relative_path: relativePath, crs }),
      timeout: 60_000,
    })
  },

  dataset(id: string, signal?: AbortSignal) {
    return json<DatasetDetail>(`/api/datasets/${encodeURIComponent(id)}`, { signal })
  },

  unregisterDataset(id: string) {
    return json<{ id: string; removed: boolean; source_deleted: false; detail: string }>(
      `/api/datasets/${encodeURIComponent(id)}`,
      {
      method: 'DELETE',
      timeout: 30_000,
      },
    )
  },

  route(id: string, signal?: AbortSignal) {
    return json<RouteResponse>(`/api/datasets/${encodeURIComponent(id)}/route`, {
      signal,
      timeout: 30_000,
    })
  },

  frames(id: string, offset: number, limit: number, track?: string, signal?: AbortSignal) {
    return json<FramePage>(
      buildApiUrl(`/api/datasets/${encodeURIComponent(id)}/frames`, {
        offset,
        limit,
        track,
      }),
      { signal },
    )
  },

  locateFrame(
    id: string,
    payload: { image_name?: string; dataset_position?: [number, number] },
    signal?: AbortSignal,
  ) {
    return json<FrameLocateResponse>(`/api/datasets/${encodeURIComponent(id)}/frames/locate`, {
      method: 'POST',
      ...jsonBody(payload),
      signal,
      timeout: 30_000,
    })
  },

  overlays(id: string, signal?: AbortSignal) {
    return json<{ items: OverlayLayer[] }>(`/api/datasets/${encodeURIComponent(id)}/overlays`, {
      signal,
      timeout: 30_000,
    })
  },

  uploadOverlay(
    id: string,
    files: File[],
    name?: string,
    crs?: string,
    encoding: OverlayEncoding = 'auto',
  ) {
    const body = new FormData()
    files.forEach((file) => body.append('files', file, file.name))
    if (name?.trim()) body.append('name', name.trim())
    if (crs?.trim()) body.append('crs', crs.trim())
    body.append('encoding', encoding)
    return json<{ layer: OverlayLayer }>(`/api/datasets/${encodeURIComponent(id)}/overlays`, {
      method: 'POST',
      body,
      timeout: 120_000,
    })
  },

  overlayFeatures(
    datasetId: string,
    layerId: string,
    coordinateSpace: OverlayCoordinateSpace,
    offset = 0,
    limit = 5_000,
    signal?: AbortSignal,
  ) {
    return json<OverlayFeatureCollection>(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/features`,
        { coordinate_space: coordinateSpace, offset, limit },
      ),
      { signal, timeout: 45_000 },
    )
  },

  overlaySpatialFeatures(
    datasetId: string,
    layerId: string,
    center: [number, number],
    radius: number,
    limit = 5_000,
    signal?: AbortSignal,
  ) {
    return json<OverlayFeatureCollection>(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/features`,
        {
          coordinate_space: 'dataset',
          center_x: center[0],
          center_y: center[1],
          radius,
          offset: 0,
          limit,
        },
      ),
      { signal, timeout: 45_000 },
    )
  },

  overlayFeature(
    datasetId: string,
    layerId: string,
    featureId: string | number,
    coordinateSpace: OverlayCoordinateSpace,
    signal?: AbortSignal,
  ) {
    return json<OverlayFeatureDetail>(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/features/${encodeURIComponent(String(featureId))}`,
        { coordinate_space: coordinateSpace },
      ),
      { signal, timeout: 30_000 },
    )
  },

  createOverlayFeature(
    datasetId: string,
    layerId: string,
    payload: OverlayFeatureCreateRequest,
  ) {
    return json<OverlayFeatureDetail>(
      `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/features`,
      { method: 'POST', ...jsonBody(payload), timeout: 30_000 },
    )
  },

  patchOverlayFeature(
    datasetId: string,
    layerId: string,
    featureId: string | number,
    payload: {
      geometry?: { type: 'Point'; coordinates: [number, number, number?] }
      coordinate_space?: OverlayCoordinateSpace
      properties?: Record<string, unknown>
      expected_revision?: number
    },
  ) {
    return json<{ feature: OverlayFeature; revision: number; coordinate_space: OverlayCoordinateSpace }>(
      `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/features/${encodeURIComponent(String(featureId))}`,
      { method: 'PATCH', ...jsonBody(payload), timeout: 30_000 },
    )
  },

  deleteOverlayFeature(
    datasetId: string,
    layerId: string,
    featureId: string | number,
    expectedRevision?: number,
  ) {
    return json<{ id: string | number; deleted: boolean; revision: number; source_preserved: boolean }>(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/features/${encodeURIComponent(String(featureId))}`,
        { expected_revision: expectedRevision },
      ),
      { method: 'DELETE', timeout: 30_000 },
    )
  },

  patchOverlay(
    datasetId: string,
    layerId: string,
    payload: {
      name?: string
      color?: string
      expected_metadata_revision: number
    },
  ) {
    return json<{ layer: OverlayLayer }>(
      `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}`,
      { method: 'PATCH', ...jsonBody(payload), timeout: 30_000 },
    )
  },

  deleteOverlay(datasetId: string, layerId: string) {
    return json<{ deleted: boolean }>(
      `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}`,
      { method: 'DELETE', timeout: 30_000 },
    )
  },

  overlayDownloadUrl(datasetId: string, layerId: string) {
    return buildApiUrl(
      `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/download`,
    )
  },

  panoramaOverlayProjection(
    datasetId: string,
    layerId: string,
    frameId: string,
    signal?: AbortSignal,
    maxDistance?: number,
  ) {
    return json<{
      layer_id: string
      frame_id: string
      coordinate_space: 'normalized_equirectangular'
      dataset_crs: string
      revision: number
      items: PanoramaOverlayFeature[]
      count: number
      detection_boxes?: PanoramaDetectionBoxObservation[]
      yaw_offset_deg: number
      pitch_offset_deg: number
    }>(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(datasetId)}/overlays/${encodeURIComponent(layerId)}/project/${encodeURIComponent(frameId)}`,
        { max_distance: maxDistance },
      ),
      { signal, timeout: 30_000 },
    )
  },

  panoramaProjectionMetadata(datasetId: string, frameId: string, signal?: AbortSignal) {
    return json<PanoramaProjectionMetadata>(
      `/api/datasets/${encodeURIComponent(datasetId)}/frames/${encodeURIComponent(frameId)}/panorama-projection`,
      { signal, timeout: 30_000 },
    )
  },

  frameDetections(datasetId: string, frameId: string, signal?: AbortSignal) {
    return json<FrameDetectionResponse>(
      `/api/datasets/${encodeURIComponent(datasetId)}/frames/${encodeURIComponent(frameId)}/detections`,
      { signal, timeout: 30_000 },
    )
  },

  panoramaPick(
    datasetId: string,
    frameId: string,
    sample: { u: number; v: number; depth: number },
  ) {
    return json<{
      dataset_position: [number, number, number]
      wgs84?: { lon: number; lat: number; altitude?: number }
    }>(`/api/datasets/${encodeURIComponent(datasetId)}/frames/${encodeURIComponent(frameId)}/panorama-pick`, {
      method: 'POST',
      ...jsonBody(sample),
      timeout: 30_000,
    })
  },

  async panorama(id: string, frameId: string, width: number, signal?: AbortSignal) {
    const response = await request(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(id)}/panoramas/${encodeURIComponent(frameId)}`,
        { width },
      ),
      { signal, timeout: 30_000 },
    )
    if (response.headers.get('content-type')?.includes('application/json')) {
      const payload = (await response.json()) as { url: string }
      return { kind: 'url' as const, value: payload.url }
    }
    return { kind: 'blob' as const, value: await response.blob() }
  },

  async points(
    id: string,
    frameId: string,
    budget: number,
    radius: number,
    signal?: AbortSignal,
  ) {
    const response = await request(
      buildApiUrl(`/api/datasets/${encodeURIComponent(id)}/points/${encodeURIComponent(frameId)}`, {
        budget,
        radius,
      }),
      {
        signal,
        timeout: 45_000,
        headers: { Accept: 'application/vnd.mmsp, application/octet-stream' },
      },
    )
    if (response.status === 202) {
      let message = '포인트 미리보기를 준비하고 있습니다.'
      try {
        const payload = (await response.json()) as { message?: string; detail?: string }
        message = payload.message ?? payload.detail ?? message
      } catch {
        // Keep the useful default.
      }
      throw new ApiError(message, 202, 'INDEXING')
    }
    return response.arrayBuffer()
  },

  async panoramaPoints(
    id: string,
    frameId: string,
    budget: number,
    radius: number,
    signal?: AbortSignal,
  ) {
    const response = await request(
      buildApiUrl(
        `/api/datasets/${encodeURIComponent(id)}/panorama-points/${encodeURIComponent(frameId)}`,
        { budget, radius },
      ),
      {
        signal,
        timeout: 45_000,
        headers: { Accept: 'application/vnd.mmso, application/octet-stream' },
      },
    )
    if (response.status === 202) {
      let message = '파노라마 포인트 인덱스를 준비하고 있습니다.'
      try {
        const payload = (await response.json()) as { message?: string; detail?: string }
        message = payload.message ?? payload.detail ?? message
      } catch {
        // Keep the useful default.
      }
      throw new ApiError(message, 202, 'INDEXING')
    }
    return response.arrayBuffer()
  },

  optimize(payload: RunRequest, signal?: AbortSignal) {
    return json<{
      parameters: RunRequest['parameters']
    }>('/api/optimize', {
      method: 'POST',
      ...jsonBody(payload),
      signal,
      timeout: 120_000,
    })
  },

  runs(signal?: AbortSignal) {
    return json<{ items: RunRecord[] }>('/api/runs', { signal })
  },

  runResults(runId: string, signal?: AbortSignal) {
    return json<RunResults>(`/api/runs/${encodeURIComponent(runId)}/results`, {
      signal,
      timeout: 30_000,
    })
  },

  importRunShapefile(
    runId: string,
    path: string,
    name?: string,
    encoding: OverlayEncoding = 'auto',
  ) {
    return json<{ layer: OverlayLayer }>(`/api/runs/${encodeURIComponent(runId)}/shapefile/import`, {
      method: 'POST',
      ...jsonBody({ path, name, encoding }),
      timeout: 60_000,
    })
  },

  createRun(payload: RunRequest) {
    return json<RunRecord>('/api/runs', {
      method: 'POST',
      ...jsonBody(payload),
      timeout: 30_000,
    })
  },

  cancelRun(runId: string) {
    return json<RunRecord>(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      ...jsonBody({}),
    })
  },

  deleteRun(runId: string) {
    return json<{
      id: string
      dismissed: boolean
      artifacts_preserved: boolean
      detail: string
    }>(`/api/runs/${encodeURIComponent(runId)}`, {
      method: 'DELETE',
    })
  },

  subscribeToRun(
    runId: string,
    onEvent: (event: RunEvent) => void,
    onConnectionError?: () => void,
  ) {
    const source = new EventSource(buildApiUrl(`/api/runs/${encodeURIComponent(runId)}/events`))
    source.onmessage = (event) => {
      try {
        onEvent(JSON.parse(event.data) as RunEvent)
      } catch {
        // Ignore heartbeat or malformed server messages and keep the stream alive.
      }
    }
    source.addEventListener('run', (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as
          | RunRecord
          | { run: RunRecord }
        onEvent({
          type: 'snapshot',
          run: 'run' in payload ? payload.run : payload,
        })
      } catch {
        // A later snapshot or status event will reconcile state.
      }
    })
    ;['progress', 'stage', 'completed', 'failed', 'cancelled'].forEach((type) => {
      source.addEventListener(type, (event) => {
        try {
          onEvent({ ...(JSON.parse((event as MessageEvent<string>).data) as RunEvent), type } as RunEvent)
        } catch {
          // The next snapshot will reconcile state.
        }
      })
    })
    source.onerror = () => onConnectionError?.()
    return () => source.close()
  },

  createUpload(name: string, files: UploadManifestFile[], rootId?: string) {
    return json<UploadSession>('/api/uploads', {
      method: 'POST',
      ...jsonBody({ name, files, root_id: rootId }),
      timeout: 30_000,
    })
  },

  async uploadedBytes(sessionId: string, fileId: string): Promise<number> {
    const response = await request(
      `/api/uploads/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}`,
      { method: 'HEAD', retries: 1 },
    )
    return Number(response.headers.get('Upload-Offset') ?? response.headers.get('X-Uploaded-Bytes') ?? 0)
  },

  async uploadChunk(
    sessionId: string,
    fileId: string,
    path: string,
    chunk: Blob,
    start: number,
    total: number,
    signal?: AbortSignal,
  ) {
    await request(`/api/uploads/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileId)}`, {
      method: 'PUT',
      body: chunk,
      headers: {
        'Content-Type': 'application/octet-stream',
        'Content-Range': `bytes ${start}-${start + chunk.size - 1}/${total}`,
        'X-Relative-Path': encodeURIComponent(path),
      },
      signal,
      timeout: 120_000,
    })
  },

  completeUpload(sessionId: string) {
    return json<{ root_id: string; relative_path: string; upload_id: string }>(
      `/api/uploads/${encodeURIComponent(sessionId)}/complete`,
      { method: 'POST', ...jsonBody({}), timeout: 60_000 },
    )
  },
}
