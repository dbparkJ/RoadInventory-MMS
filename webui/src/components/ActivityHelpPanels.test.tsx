import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunRecord } from '../types'
import { ActivityPanel, HelpPanel } from './ActivityHelpPanels'

const RUN: RunRecord = {
  id: 'run-1',
  dataset_id: 'dataset-1',
  dataset_name: 'MMS 구간 1',
  status: 'running',
  progress: 42,
  stage: '객체 검출',
  created_at: '2026-08-03T09:30:00.000Z',
}

afterEach(cleanup)

describe('ActivityPanel', () => {
  it('shows current work and recent notifications and opens the queue', () => {
    const onOpenQueue = vi.fn()
    render(
      <ActivityPanel
        runs={[RUN]}
        alerts={[{ id: 'alert-1', tone: 'success', title: '인덱싱 완료' }]}
        detached={false}
        onClose={vi.fn()}
        onOpenQueue={onOpenQueue}
      />,
    )

    expect(screen.getByRole('dialog', { name: '알림' })).toBeInTheDocument()
    expect(screen.getByText('인덱싱 완료')).toBeInTheDocument()
    expect(screen.getByText('MMS 구간 1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '실행 큐 열기' }))
    expect(onOpenQueue).toHaveBeenCalledOnce()
  })
})

describe('HelpPanel', () => {
  it('documents navigation shortcuts and closes with Escape', () => {
    const onClose = vi.fn()
    render(<HelpPanel detached={false} onClose={onClose} />)

    expect(screen.getByRole('dialog', { name: '도움말' })).toBeInTheDocument()
    expect(screen.getByText('이전 프레임')).toBeInTheDocument()
    expect(screen.getByText('다음 프레임')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })
})
