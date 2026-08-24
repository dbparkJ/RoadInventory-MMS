import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
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
  it('shows detection and manual-object help without exposing review workspace UI', () => {
    const onClose = vi.fn()
    render(<HelpPanel detached={false} onClose={onClose} />)

    expect(screen.getByRole('dialog', { name: '도움말' })).toBeInTheDocument()
    expect(screen.getByText('검출 실행 빠른 순서')).toBeInTheDocument()
    expect(screen.getByText('이전 프레임')).toBeInTheDocument()
    expect(screen.getByText('다음 프레임')).toBeInTheDocument()
    expect(screen.getByText('지주 바닥점 산출·확정')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: '운영자 작업 안내' })).not.toBeInTheDocument()
    expect(screen.queryByText(/검수 작업/)).not.toBeInTheDocument()
    expect(screen.queryByText('QA 오류 탐색기 열기·닫기')).not.toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('traps Tab within an attached modal and restores the opener on unmount', () => {
    const opener = document.createElement('button')
    opener.textContent = '도움말 열기'
    document.body.appendChild(opener)
    opener.focus()
    const view = render(<HelpPanel detached={false} onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: '도움말' })
    const focusable = within(dialog).getAllByRole('button')
    const first = focusable[0]
    const last = focusable[focusable.length - 1]

    expect(dialog).toHaveFocus()
    last.focus()
    const forward = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    })
    window.dispatchEvent(forward)
    expect(forward.defaultPrevented).toBe(true)
    expect(first).toHaveFocus()

    first.focus()
    const backward = new KeyboardEvent('keydown', {
      key: 'Tab',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    })
    window.dispatchEvent(backward)
    expect(backward.defaultPrevented).toBe(true)
    expect(last).toHaveFocus()

    opener.focus()
    const reenter = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    })
    window.dispatchEvent(reenter)
    expect(reenter.defaultPrevented).toBe(true)
    expect(first).toHaveFocus()

    view.unmount()
    expect(opener).toHaveFocus()
    opener.remove()
  })

  it('restores the prior opener immediately when the attached close action runs', () => {
    const opener = document.createElement('button')
    opener.textContent = '도움말 열기'
    document.body.appendChild(opener)
    opener.focus()
    const onClose = vi.fn()
    const view = render(<HelpPanel detached={false} onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: '도움말 닫기' }))

    expect(onClose).toHaveBeenCalledOnce()
    expect(opener).toHaveFocus()
    view.unmount()
    opener.remove()
  })

  it('does not trap Tab when the panel is detached', () => {
    const view = render(<HelpPanel detached onClose={vi.fn()} />)
    const dialog = screen.getByRole('dialog', { name: '도움말' })
    const focusable = within(dialog).getAllByRole('button')
    const last = focusable[focusable.length - 1]
    last.focus()
    const tab = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    })

    window.dispatchEvent(tab)

    expect(tab.defaultPrevented).toBe(false)
    expect(last).toHaveFocus()
    expect(dialog).not.toHaveAttribute('aria-modal')
    view.unmount()
  })
})
