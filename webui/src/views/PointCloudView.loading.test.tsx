import { act, render, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from '../lib/api'
import * as mmsp from '../lib/mmsp'
import type { Frame, FrameDetectionResponse } from '../types'
import PointCloudView from './PointCloudView'

const EMPTY_DETECTIONS: FrameDetectionResponse = {
  dataset_id: 'dataset', frame_id: 'frame',
  coordinate_space: 'panorama_equirectangular_pixels', projection: 'equirectangular',
  items: [], count: 0, model_count: 0, truncated: false,
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('does not parse a point body that finishes after its viewer was closed', async () => {
  let finish!: (buffer: ArrayBuffer) => void
  const points = vi.spyOn(api, 'points').mockImplementation(() => new Promise((resolve) => {
    finish = resolve
  }))
  vi.spyOn(api, 'panoramaProjectionMetadata').mockRejectedValue(new Error('No synthetic camera'))
  vi.spyOn(api, 'frameDetections').mockResolvedValue(EMPTY_DETECTIONS)
  const parse = vi.spyOn(mmsp, 'parseMmsp')
  const frame: Frame = {
    id: 'frame', index: 0, track_id: 'track', timestamp: '',
    coordinate: null, has_panorama: false, has_points: true,
  }
  const viewer = render(<PointCloudView datasetId="dataset" frame={frame} demoMode={false} />)
  await waitFor(() => expect(points).toHaveBeenCalledOnce())
  const signal = points.mock.calls[0][3]
  viewer.unmount()
  expect(signal?.aborted).toBe(true)
  await act(async () => finish(new ArrayBuffer(0)))
  expect(parse).not.toHaveBeenCalled()
})

it('keeps polling a point catalog that is still preparing after 50 seconds', async () => {
  vi.useFakeTimers()
  const points = vi.spyOn(api, 'points').mockRejectedValue(new ApiError('Preparing', 202, 'INDEXING'))
  vi.spyOn(api, 'panoramaProjectionMetadata').mockRejectedValue(new Error('No synthetic camera'))
  vi.spyOn(api, 'frameDetections').mockResolvedValue(EMPTY_DETECTIONS)
  const frame: Frame = {
    id: 'frame', index: 0, track_id: 'track', timestamp: '',
    coordinate: null, has_panorama: false, has_points: true,
  }
  const viewer = render(<PointCloudView datasetId="dataset" frame={frame} demoMode={false} />)
  try {
    await act(() => vi.advanceTimersByTimeAsync(52_000))
    expect(points).toHaveBeenCalledTimes(27)
  } finally {
    viewer.unmount()
  }
})
