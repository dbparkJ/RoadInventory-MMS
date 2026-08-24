import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../lib/api'
import type { ManualObjectContextValue } from './ManualObjectContext'
import type { OverlayContextValue } from './OverlayContext'

const workspaceMocks = vi.hoisted(() => ({
  manual: null as ManualObjectContextValue | null,
  overlay: null as OverlayContextValue | null,
}))

vi.mock('./ManualObjectContext', () => ({
  useOptionalManualObjectWorkspace: () => workspaceMocks.manual,
}))

vi.mock('./OverlayContext', () => ({
  poleBaseReasonMessages: (codes: string[]) => codes,
  poleBaseTemplateValidationBlocksSave: () => false,
  useOptionalOverlayWorkspace: () => workspaceMocks.overlay,
}))

vi.mock('./ReviewContext', () => ({
  useOptionalReviewWorkspace: () => null,
}))

import { ProposalInspector } from './ProposalInspector'

const GEOMETRY = {
  type: 'equirectangular_bbox' as const,
  u_intervals: [[0.2, 0.4]] as Array<[number, number]>,
  v_min: 0.3,
  v_max: 0.6,
  image_width: 4_096,
  image_height: 2_048,
}

function manualWorkspace(): ManualObjectContextValue {
  return {
    enabled: true,
    targetLayer: {
      id: 'traffic-layer',
      dataset_id: 'dataset-1',
      name: 'Traffic signs',
      geometry_type: 'Point',
      feature_count: 0,
      revision: 1,
    },
    proposalState: { status: 'idle' },
    templateId: 'TRAFFIC_SIGN',
  } as unknown as ManualObjectContextValue
}

function overlayWorkspace(): OverlayContextValue {
  return {
    datasetId: 'dataset-1',
    poleBaseProposal: { status: 'idle' },
    features: {},
    selected: null,
  } as unknown as OverlayContextValue
}

beforeEach(() => {
  workspaceMocks.manual = manualWorkspace()
  workspaceMocks.overlay = overlayWorkspace()
  vi.spyOn(api, 'overlayEditHistory').mockResolvedValue({ items: [] })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ProposalInspector active feedback', () => {
  it('automatically reveals active proposal feedback even if the idle inspector was collapsed', async () => {
    const view = render(<ProposalInspector />)
    await waitFor(() => expect(api.overlayEditHistory).toHaveBeenCalled())

    fireEvent.click(screen.getByRole('button', { name: '제안 검사기 접기' }))
    expect(screen.queryByText('편집 이력이 없습니다.')).not.toBeInTheDocument()

    workspaceMocks.manual = {
      ...workspaceMocks.manual!,
      proposalState: {
        status: 'loading',
        frameId: 'frame-1',
        geometry: GEOMETRY,
        preparingAttempt: 2,
        preparingMaxAttempts: 8,
      },
      cancel: vi.fn(),
    }
    view.rerender(<ProposalInspector />)

    expect(screen.getByText('3D 계산 중')).toBeInTheDocument()
    expect(screen.getByText('원본 점군 준비 중 · 자동 재시도 2/8')).toBeInTheDocument()
    const collapse = screen.getByRole('button', { name: '제안 검사기 접기' })
    expect(collapse).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(collapse)
    expect(screen.getByText('원본 점군 준비 중 · 자동 재시도 2/8')).toBeInTheDocument()
  })
})
