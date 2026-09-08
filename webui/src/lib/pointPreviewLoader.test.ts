import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'
import { loadPointPreview } from './pointPreviewLoader'

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

function onePoint(): ArrayBuffer {
  const bytes = new ArrayBuffer(55)
  const view = new DataView(bytes)
  view.setUint32(0, 0x50534d4d, true)
  view.setUint16(4, 1, true)
  view.setUint16(6, 1, true)
  view.setUint32(8, 1, true)
  for (let axis = 0; axis < 3; axis += 1) {
    view.setFloat32(12 + axis * 4, axis + 1, true)
    view.setFloat32(24 + axis * 4, axis + 1, true)
    view.setFloat32(40 + axis * 4, axis + 1, true)
    view.setUint8(52 + axis, 8 + axis)
  }
  return bytes
}

it('loads a catalog ready after 50.6 seconds without changing point values', async () => {
  const readyAt = Date.now() + 50_600
  const bytes = onePoint()
  const points = vi.spyOn(api, 'points').mockImplementation(async () => {
    if (Date.now() < readyAt) throw new ApiError('Preparing', 202, 'INDEXING')
    return bytes
  })
  const controller = new AbortController()
  const pending = loadPointPreview('dataset', 'frame', 250_000, { signal: controller.signal })
  await vi.advanceTimersByTimeAsync(52_000)
  const payload = await pending
  expect(points).toHaveBeenCalledTimes(27)
  expect(payload.pointCount).toBe(1)
  expect(Array.from(payload.positions)).toEqual([1, 2, 3])
  expect(Array.from(payload.colors!)).toEqual([8, 9, 10])
  expect(payload.bounds).toEqual({ min: [1, 2, 3], max: [1, 2, 3] })
  expect(vi.getTimerCount()).toBe(0)
})

it('stops indexing at the 120-second total deadline', async () => {
  const points = vi.spyOn(api, 'points').mockRejectedValue(new ApiError('Preparing', 202, 'INDEXING'))
  const controller = new AbortController()
  const pending = loadPointPreview('dataset', 'frame', 250_000, { signal: controller.signal })
  const outcome = pending.catch((error: unknown) => error)
  await vi.advanceTimersByTimeAsync(120_000)
  expect(await outcome).toMatchObject({ code: 'POINT_PREVIEW_TIMEOUT' })
  expect(points).toHaveBeenCalledTimes(60)
  expect(vi.getTimerCount()).toBe(0)
})

it('includes an in-flight body after indexing in the same total deadline', async () => {
  const points = vi.spyOn(api, 'points')
    .mockRejectedValueOnce(new ApiError('Preparing', 202, 'INDEXING'))
    .mockImplementation((_dataset, _frame, _budget, signal) => new Promise((_resolve, reject) => {
      signal!.addEventListener('abort', () => reject(signal!.reason), { once: true })
    }))
  const controller = new AbortController()
  const pending = loadPointPreview('dataset', 'frame', 250_000, { signal: controller.signal })
  const outcome = pending.catch((error: unknown) => error)
  await vi.advanceTimersByTimeAsync(120_000)
  expect(await outcome).toMatchObject({ code: 'POINT_PREVIEW_TIMEOUT' })
  expect(points).toHaveBeenCalledTimes(2)
  expect(points.mock.calls[1][3]?.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
})

it('cancels indexing backoff immediately when the frame changes', async () => {
  const points = vi.spyOn(api, 'points').mockRejectedValue(new ApiError('Preparing', 202, 'INDEXING'))
  const controller = new AbortController()
  const removed = vi.spyOn(controller.signal, 'removeEventListener')
  const pending = loadPointPreview('dataset', 'frame', 250_000, { signal: controller.signal })
  const outcome = pending.catch((error: unknown) => error)
  await vi.advanceTimersByTimeAsync(1_000)
  controller.abort()
  expect(await outcome).toMatchObject({ name: 'AbortError' })
  await vi.advanceTimersByTimeAsync(10_000)
  expect(points).toHaveBeenCalledTimes(1)
  expect(removed).toHaveBeenCalledWith('abort', expect.any(Function))
  expect(vi.getTimerCount()).toBe(0)
})

it('does not start a point request after its viewer has already closed', async () => {
  const points = vi.spyOn(api, 'points')
  const controller = new AbortController()
  controller.abort()
  await expect(loadPointPreview('dataset', 'frame', 250_000, { signal: controller.signal }))
    .rejects.toMatchObject({ name: 'AbortError' })
  expect(points).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
})
