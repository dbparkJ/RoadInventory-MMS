import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { OverlayFeatureCollection, OverlayLayer } from '../types'
import { DatasetOverlayBrowser } from './DatasetOverlayBrowser'
import { OverlayProvider } from './OverlayContext'

const LAYERS: OverlayLayer[] = [
  {
    id: 'layer-signs',
    dataset_id: 'dataset-1',
    name: '교통안전표지',
    color: '#123456',
    geometry_type: 'Point',
    feature_count: 1,
    revision: 2,
  },
  {
    id: 'layer-poles',
    dataset_id: 'dataset-1',
    name: '지주',
    geometry_type: 'Point',
    feature_count: 1,
    revision: 3,
  },
]

const EMPTY_PAGE: OverlayFeatureCollection = {
  type: 'FeatureCollection',
  features: [],
  fields: [],
  total: 0,
  offset: 0,
  limit: 3_000,
  revision: 1,
  next_offset: null,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('DatasetOverlayBrowser', () => {
  it('shows only searchable layer visibility controls without an attribute table or edit actions', async () => {
    vi.spyOn(api, 'overlays').mockResolvedValue({ items: LAYERS })
    vi.spyOn(api, 'overlayFeatures').mockResolvedValue(EMPTY_PAGE)

    render(
      <OverlayProvider datasetId="dataset-1" demoMode={false}>
        <DatasetOverlayBrowser focusLayerId="layer-signs" />
      </OverlayProvider>,
    )

    const signs = await screen.findByRole('button', { name: '교통안전표지 레이어 선택' })
    expect(screen.getByRole('button', { name: '지주 레이어 선택' })).toBeInTheDocument()
    expect(signs.querySelector('i')).toHaveStyle({ background: '#123456' })
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText('속성표')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /신규 포인트/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /위치만 복사/ })).not.toBeInTheDocument()

    const signsVisibility = screen.getByRole('button', { name: '교통안전표지 숨기기' })
    expect(signsVisibility).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(signsVisibility)
    expect(screen.getByRole('button', { name: '교통안전표지 표시' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )

    fireEvent.click(screen.getByRole('button', { name: '지주 레이어 선택' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '지주 레이어 선택' }).closest('[role="listitem"]'))
        .toHaveClass('active'),
    )

    fireEvent.change(screen.getByRole('textbox', { name: 'SHP 레이어 검색' }), {
      target: { value: '교통' },
    })
    expect(screen.getByRole('button', { name: '교통안전표지 레이어 선택' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '지주 레이어 선택' })).not.toBeInTheDocument()
  })
})
