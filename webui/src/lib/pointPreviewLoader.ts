import { api, ApiError, type PointPreviewFocus } from './api'
import { parseMmsp } from './mmsp'
import type { PointCloudPayload } from '../types'

function waitForPointIndex(signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, 2_000)
    const abort = () => {
      window.clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      reject(signal.reason)
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}

/** Load one frame within a total deadline, including indexing and body transfer. */
export async function loadPointPreview(
  datasetId: string,
  frameId: string,
  budget: number,
  options: {
    signal: AbortSignal
    colorMode?: Parameters<typeof api.points>[4]
    focuses?: readonly PointPreviewFocus[]
    onIndexing?: () => void
  },
): Promise<PointCloudPayload> {
  const controller = new AbortController()
  const abort = () => controller.abort(options.signal.reason)
  if (options.signal.aborted) abort()
  else options.signal.addEventListener('abort', abort, { once: true })
  const deadline = window.setTimeout(() => controller.abort(new ApiError(
    '포인트 미리보기 준비 시간이 초과되었습니다. 다시 시도해 주세요.',
    0,
    'POINT_PREVIEW_TIMEOUT',
  )), 120_000)
  try {
    while (true) {
      if (controller.signal.aborted) throw controller.signal.reason
      try {
        const buffer = await api.points(
          datasetId, frameId, budget, controller.signal,
          options.colorMode, options.focuses,
        )
        // A closed viewer must not unpack a late 3.75–15 MB response.
        if (controller.signal.aborted) throw controller.signal.reason
        return parseMmsp(buffer)
      } catch (reason) {
        if (controller.signal.aborted) throw controller.signal.reason
        if (!(reason instanceof ApiError && reason.status === 202)) throw reason
        options.onIndexing?.()
        await waitForPointIndex(controller.signal)
      }
    }
  } finally {
    window.clearTimeout(deadline)
    options.signal.removeEventListener('abort', abort)
  }
}
