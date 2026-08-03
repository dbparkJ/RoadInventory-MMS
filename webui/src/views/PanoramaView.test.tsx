import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { Frame } from '../types'
import PanoramaView, { panoramaRequestWidth } from './PanoramaView'

vi.mock('../lib/api', () => ({
  api: {
    panorama: vi.fn(() => new Promise(() => undefined)),
  },
}))

const FRAME: Frame = {
  id: 'frame-12',
  index: 12,
  track_id: 'track-1',
  timestamp: '2026-08-03T09:30:00.000Z',
  coordinate: { lon: 126.978, lat: 37.5665 },
  has_panorama: true,
  has_points: true,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('panoramaRequestWidth', () => {
  it('scales with the viewport while enforcing each quality budget', () => {
    expect(panoramaRequestWidth(640, 1, 'high')).toBe(4096)
    expect(panoramaRequestWidth(900, 1, 'high')).toBe(4096)
    expect(panoramaRequestWidth(1920, 2, 'high')).toBe(4096)
    expect(panoramaRequestWidth(640, 1, 'ultra')).toBe(8192)
    expect(panoramaRequestWidth(1280, 1, 'fast')).toBe(1920)
    expect(panoramaRequestWidth(1920, 2, 'fast')).toBe(2048)
  })
})

describe('PanoramaView frame navigation', () => {
  it('navigates with directional controls and focused-view arrow keys', () => {
    const onPreviousFrame = vi.fn()
    const onNextFrame = vi.fn()

    const { getByRole } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        onPreviousFrame={onPreviousFrame}
        onNextFrame={onNextFrame}
      />,
    )

    fireEvent.click(getByRole('button', { name: '이전 프레임으로 이동' }))
    fireEvent.click(getByRole('button', { name: '다음 프레임으로 이동' }))
    const viewer = getByRole('region', { name: '파노라마 뷰어' })
    fireEvent.keyDown(viewer, { key: 'ArrowLeft' })
    fireEvent.keyDown(viewer, { key: 'ArrowRight' })

    expect(onPreviousFrame).toHaveBeenCalledTimes(2)
    expect(onNextFrame).toHaveBeenCalledTimes(2)
  })

  it('uses high quality by default and reloads with the fast quality budget', async () => {
    const { getByRole } = render(
      <PanoramaView datasetId="dataset-1" frame={FRAME} demoMode={false} />,
    )

    await waitFor(() => {
      expect(api.panorama).toHaveBeenLastCalledWith(
        'dataset-1',
        'frame-12',
        4096,
        expect.any(AbortSignal),
      )
    })

    fireEvent.change(getByRole('combobox', { name: '파노라마 화질' }), {
      target: { value: 'fast' },
    })

    await waitFor(() => {
      expect(api.panorama).toHaveBeenLastCalledWith(
        'dataset-1',
        'frame-12',
        1920,
        expect.any(AbortSignal),
      )
    })
  })

  it('does not navigate beyond the available frame range', () => {
    const onPreviousFrame = vi.fn()
    const onNextFrame = vi.fn()

    const { getByRole } = render(
      <PanoramaView
        datasetId="dataset-1"
        frame={FRAME}
        demoMode={false}
        onPreviousFrame={onPreviousFrame}
        onNextFrame={onNextFrame}
        hasPreviousFrame={false}
        hasNextFrame={false}
      />,
    )

    const previous = getByRole('button', { name: '이전 프레임으로 이동' })
    const next = getByRole('button', { name: '다음 프레임으로 이동' })
    expect(previous).toBeDisabled()
    expect(next).toBeDisabled()
    fireEvent.click(previous)
    fireEvent.click(next)
    const viewer = getByRole('region', { name: '파노라마 뷰어' })
    fireEvent.keyDown(viewer, { key: 'ArrowLeft' })
    fireEvent.keyDown(viewer, { key: 'ArrowRight' })

    expect(onPreviousFrame).not.toHaveBeenCalled()
    expect(onNextFrame).not.toHaveBeenCalled()
  })
})
