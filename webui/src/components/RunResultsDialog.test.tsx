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
  archives: {
    all: {
      url: '/api/runs/run-42/archive?scope=all',
      filename: 'run-42-all-results.zip',
    },
    detected_images: {
      url: '/api/runs/run-42/archive?scope=detected-images',
      filename: 'run-42-detected-images.zip',
    },
  },
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
  it('shows a selected-dataset empty state and routes to the execution queue', () => {
    const onOpenQueue = vi.fn()
    const runResults = vi.spyOn(api, 'runResults')

    render(
      <RunResultsDialog
        run={null}
        onClose={vi.fn()}
        emptyState={{ open: true, datasetName: '강남 검출 구간', onOpenQueue }}
      />,
    )

    expect(screen.getByText('강남 검출 구간 · 최신 완료 실행')).toBeInTheDocument()
    expect(screen.getByText('완료된 자동 검출결과가 없습니다')).toBeInTheDocument()
    expect(runResults).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '실행 큐 확인' }))
    expect(onOpenQueue).toHaveBeenCalledOnce()
  })

  it('offers the two server archives from one ZIP menu without individual SHP downloads', async () => {
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)

    render(<RunResultsDialog run={RUN} onClose={vi.fn()} />)

    expect(await screen.findByText('runs/run-42/output')).toBeInTheDocument()
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /ZIP 받기/ })).not.toBeInTheDocument()
    expect(document.querySelector(`a[href*="/shapefile?"]`)).not.toBeInTheDocument()

    const trigger = screen.getByRole('button', { name: /ZIP 받기/ })
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(trigger)

    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('menu', { name: 'ZIP 종류 선택' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /전체 산출물/ })).toHaveAttribute(
      'href',
      '/api/runs/run-42/archive?scope=all',
    )
    expect(screen.getByRole('menuitem', { name: /검출된 사진/ })).toHaveAttribute(
      'href',
      '/api/runs/run-42/archive?scope=detected-images',
    )
    expect(screen.queryByRole('link', { name: /detected_signs\.dbf/ })).not.toBeInTheDocument()
    expect(document.querySelector('.result-shapefile-list')).toBeInTheDocument()
  })

  it('portals the viewport layer outside a filtered trigger ancestor', () => {
    const { container } = render(
      <div style={{ backdropFilter: 'blur(8px)' }}>
        <RunResultsDialog
          run={null}
          onClose={vi.fn()}
          emptyState={{ open: true, datasetName: '강남 검출 구간' }}
        />
      </div>,
    )

    expect(container.querySelector('.result-dialog-layer')).not.toBeInTheDocument()
    expect(document.body.querySelector(':scope > .result-dialog-layer')).toBeInTheDocument()
  })

  it('closes the ZIP menu with Escape or an outside pointer and restores trigger focus', async () => {
    vi.spyOn(api, 'runResults').mockResolvedValue(RESULTS)
    render(<RunResultsDialog run={RUN} onClose={vi.fn()} />)
    const trigger = await screen.findByRole('button', { name: /ZIP 받기/ })

    fireEvent.click(trigger)
    const firstOption = screen.getByRole('menuitem', { name: /전체 산출물/ })
    expect(firstOption).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()

    fireEvent.click(trigger)
    expect(screen.getByRole('menu')).toBeInTheDocument()
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
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
