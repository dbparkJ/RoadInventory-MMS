import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunRecord, RunStatus } from '../types'
import { RunQueue } from './RunQueue'

afterEach(cleanup)

function run(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    id: 'run-unknown-status',
    dataset_id: 'dataset-1',
    dataset_name: '테스트 데이터셋',
    status: 'queued',
    progress: 10,
    created_at: '2026-08-04T00:00:00.000Z',
    ...overrides,
  }
}

describe('RunQueue', () => {
  it('renders an unknown server status with a safe fallback', () => {
    const unknown = run({ status: 'toString' as RunStatus, progress: Number.NaN })

    const { container } = render(
      <RunQueue runs={[unknown]} open onClose={vi.fn()} onCancel={vi.fn()} />,
    )

    expect(screen.getByText('알 수 없는 상태')).toBeInTheDocument()
    expect(screen.getByText('0%')).toBeInTheDocument()
    expect(container.querySelector('.run-card')).toHaveClass('status-unknown')
    expect(container.querySelector('.progress-track > span')).toHaveStyle({ width: '0%' })
  })

  it('uses canonical metadata when a new legacy status is returned', () => {
    const retrying = run({
      status: 'worker-restart' as RunStatus,
      canonical_status: 'retrying',
      current_stage: 'load_or_build_spatial_index',
      progress: 22,
    })

    render(<RunQueue runs={[retrying]} open onClose={vi.fn()} onCancel={vi.fn()} />)

    expect(screen.getByText('재시도 중')).toBeInTheDocument()
    expect(screen.getByText('load_or_build_spatial_index')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '취소' })).toBeInTheDocument()
  })

  it('prefers a more specific canonical status over the legacy queue status', () => {
    render(
      <RunQueue
        runs={[run({ status: 'running', canonical_status: 'retrying' })]}
        open
        onClose={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    expect(screen.getByText('재시도 중')).toBeInTheDocument()
  })

  it('recognizes the internal starting state without throwing', () => {
    render(
      <RunQueue
        runs={[run({ status: 'starting' as RunStatus })]}
        open
        onClose={vi.fn()}
        onCancel={vi.fn()}
      />,
    )

    expect(screen.getByText('시작 중')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '취소' })).toBeInTheDocument()
  })
})
