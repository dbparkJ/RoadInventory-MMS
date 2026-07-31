interface UploadFileOptions {
  file: Blob
  chunkSize: number
  initialOffset?: number
  confirmInitialOffset?: boolean
  signal?: AbortSignal
  maxRetries?: number
  getUploadedBytes: () => Promise<number>
  putChunk: (chunk: Blob, start: number, total: number) => Promise<void>
  onOffsetChange?: (nextOffset: number, previousOffset: number) => void
  wait?: (attempt: number) => Promise<void>
}

function assertOffset(offset: number, total: number): number {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > total) {
    throw new Error('서버가 올바르지 않은 업로드 오프셋을 반환했습니다.')
  }
  return offset
}

function abortError(signal?: AbortSignal): DOMException {
  return signal?.reason instanceof DOMException
    ? signal.reason
    : new DOMException('Cancelled', 'AbortError')
}

export async function uploadFileWithResume({
  file,
  chunkSize,
  initialOffset = 0,
  confirmInitialOffset = false,
  signal,
  maxRetries = 3,
  getUploadedBytes,
  putChunk,
  onOffsetChange,
  wait = async (attempt) => {
    await new Promise((resolve) => window.setTimeout(resolve, Math.min(2_500, 400 * 2 ** attempt)))
  },
}: UploadFileOptions): Promise<number> {
  if (!Number.isSafeInteger(chunkSize) || chunkSize <= 0) {
    throw new Error('서버가 올바르지 않은 청크 크기를 반환했습니다.')
  }
  if (!Number.isSafeInteger(maxRetries) || maxRetries < 0) {
    throw new Error('업로드 재시도 횟수가 올바르지 않습니다.')
  }

  let offset = assertOffset(initialOffset, file.size)
  const updateOffset = (nextOffset: number) => {
    const validated = assertOffset(nextOffset, file.size)
    const previous = offset
    offset = validated
    if (previous !== validated) onOffsetChange?.(validated, previous)
  }

  if (confirmInitialOffset) {
    updateOffset(await getUploadedBytes())
  }

  while (offset < file.size) {
    if (signal?.aborted) throw abortError(signal)
    let failures = 0

    while (true) {
      if (signal?.aborted) throw abortError(signal)
      const start = offset
      const end = Math.min(file.size, start + chunkSize)
      try {
        await putChunk(file.slice(start, end), start, file.size)
        updateOffset(end)
        break
      } catch (uploadError) {
        if (signal?.aborted) throw abortError(signal)
        failures += 1

        try {
          updateOffset(await getUploadedBytes())
        } catch {
          // A transient HEAD failure still consumes this bounded retry attempt.
        }
        if (offset >= file.size) break
        if (offset !== start) {
          failures = 0
          continue
        }
        if (failures > maxRetries) throw uploadError
        await wait(failures)
      }
    }
  }

  return offset
}

export function uploadManifestKey(
  files: Array<{ path: string; size: number; lastModified: number }>,
): string {
  let hash = 0x811c9dc5
  const total = files.reduce((sum, item) => sum + item.size, 0)
  const values = files
    .map((item) => `${item.path}\0${item.size}\0${item.lastModified}`)
    .sort((first, second) => first.localeCompare(second))
  for (const value of values) {
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index)
      hash = Math.imul(hash, 0x01000193)
    }
  }
  return `${files.length}:${total}:${(hash >>> 0).toString(36)}`
}
