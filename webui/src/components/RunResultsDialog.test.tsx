import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { OverlayLayer, RunRecord, RunResults } from '../types'
import { RunResultsDialog } from './RunResultsDialog'

const RUN: RunRecord = {
  id: 'run-42',
  dataset_id: 'dataset-7',
  dataset_name: '강남 검출 구간',
  status: 'completed',
  progress: 100,
  created_at: '2026-08-03T09:30:00.000Z',
}

const RESULTS: RunResults = {
  output_location: {
    kind: 'server_managed',
    relative_path: 'runs/run-42/output',
    results_url: '/api/runs/run-42/results',
  },
  shapefiles: [
    {
      path: 'shp/detected_signs.shp',
      name: 'detected_signs',
      download_url: '/api/runs/run-42/shapefile?path=shp%2Fdetected_signs.shp',
    },
  ],
  files: [
    {
      path: 'shp/detected_signs.dbf',
      name: 'detected_signs.dbf',
      size: 2_048,
      type: 'application/octet-stream',
      url: '/api/runs/run-42/artifacts/shp/detected_signs.dbf',
    },
  ],
  file_count: 1,
}

const IMPORTED_LAYER: OverlayLayer = {
  id: 'layer-imported',
  dataset_id: RUN.dataset_id,
  name: 'detected_signs',
  geometry_type: 'Point',
  feature_count: 12,
  revision: 0,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('RunResultsDialog', () => {
  it('renders the managed output location and uses server download URLs', async () => {
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)

    render(<RunResultsDialog run={RUN} onClose={vi.fn()} />)

    expect(await screen.findByText('runs/run-42/output')).toBeInTheDocument()
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /ZIP 받기/ })).toHaveAttribute(
      'href',
      '/api/runs/run-42/shapefile?path=shp%2Fdetected_signs.shp',
    )
    expect(screen.getByRole('link', { name: /detected_signs\.dbf/ })).toHaveAttribute(
      'href',
      '/api/runs/run-42/artifacts/shp/detected_signs.dbf',
    )
  })

  it('opens an imported layer for its dataset and disables duplicate imports', async () => {
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)
    const importRunShapefile = vi
      .spyOn(api, 'importRunShapefile')
      .mockResolvedValue({ layer: IMPORTED_LAYER })
    const changed = vi.fn()
    window.addEventListener('mms-overlay-changed', changed)

    try {
      render(<RunResultsDialog run={RUN} onClose={vi.fn()} />)
      const importButton = await screen.findByRole('button', { name: /검수 레이어로 열기/ })

      fireEvent.click(importButton)

      await waitFor(() => {
        expect(importRunShapefile).toHaveBeenCalledWith(
          RUN.id,
          'shp/detected_signs.shp',
          'detected_signs',
        )
        expect(changed).toHaveBeenCalledTimes(1)
      })
      const event = changed.mock.calls[0][0] as CustomEvent
      expect(event.detail).toEqual({
        open: true,
        datasetId: RUN.dataset_id,
        layerId: IMPORTED_LAYER.id,
      })
      expect(importButton).toBeDisabled()

      fireEvent.click(importButton)
      expect(importRunShapefile).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mms-overlay-changed', changed)
    }
  })
})
