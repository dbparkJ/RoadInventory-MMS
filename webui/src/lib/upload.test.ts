import { describe, expect, it, vi } from 'vitest'
import { uploadFileWithResume, uploadManifestKey } from './upload'

describe('uploadFileWithResume', () => {
  it('uses HEAD to reconcile a chunk whose successful response was lost', async () => {
    const file = new Blob([new Uint8Array(10)])
    let serverOffset = 0
    let loseFirstResponse = true
    const starts: number[] = []
    const getUploadedBytes = vi.fn(async () => serverOffset)

    const uploaded = await uploadFileWithResume({
      file,
      chunkSize: 4,
      maxRetries: 2,
      getUploadedBytes,
      putChunk: async (chunk, start) => {
        starts.push(start)
        serverOffset = start + chunk.size
        if (loseFirstResponse) {
          loseFirstResponse = false
          throw new Error('connection closed after server commit')
        }
      },
      wait: async () => undefined,
    })

    expect(uploaded).toBe(10)
    expect(serverOffset).toBe(10)
    expect(starts).toEqual([0, 4, 8])
    expect(getUploadedBytes).toHaveBeenCalledTimes(1)
  })

  it('stops after the bounded retry count when neither PUT nor HEAD advances', async () => {
    const putChunk = vi.fn(async () => {
      throw new Error('offline')
    })
    const getUploadedBytes = vi.fn(async () => 0)

    await expect(
      uploadFileWithResume({
        file: new Blob([new Uint8Array(4)]),
        chunkSize: 4,
        maxRetries: 2,
        getUploadedBytes,
        putChunk,
        wait: async () => undefined,
      }),
    ).rejects.toThrow('offline')
    expect(putChunk).toHaveBeenCalledTimes(3)
    expect(getUploadedBytes).toHaveBeenCalledTimes(3)
  })
})

describe('uploadManifestKey', () => {
  it('is stable when the browser enumerates the same folder in another order', () => {
    const first = [
      { path: 'root/a.jpg', size: 10, lastModified: 1 },
      { path: 'root/b.csv', size: 20, lastModified: 2 },
    ]
    expect(uploadManifestKey(first)).toBe(uploadManifestKey([...first].reverse()))
  })
})
