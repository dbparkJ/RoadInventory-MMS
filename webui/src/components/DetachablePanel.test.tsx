import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useRef, useState } from 'react'
import { DetachablePanel, type DetachablePanelHandle } from './DetachablePanel'

function fakePopup() {
  const events = new EventTarget()
  const popupDocument = document.implementation.createHTMLDocument('')
  let closed = false
  const addEventListener = vi.fn(events.addEventListener.bind(events))
  const removeEventListener = vi.fn(events.removeEventListener.bind(events))
  const popup = {
    document: popupDocument,
    get closed() {
      return closed
    },
    KeyboardEvent: window.KeyboardEvent,
    addEventListener,
    removeEventListener,
    dispatchEvent: events.dispatchEvent.bind(events),
    focus: vi.fn(),
    close: vi.fn(() => {
      if (closed) return
      closed = true
      events.dispatchEvent(new Event('beforeunload'))
    }),
  } as unknown as Window
  return {
    popup,
    addEventListener,
    removeEventListener,
    forceClose: () => {
      closed = true
    },
  }
}

function Harness({ onDetachedChange }: { onDetachedChange?: (detached: boolean) => void }) {
  const panelRef = useRef<DetachablePanelHandle>(null)
  return (
    <DetachablePanel
      ref={panelRef}
      id="relay-test"
      title="relay test"
      onDetachedChange={onDetachedChange}
    >
      {() => (
        <button type="button" onClick={() => panelRef.current?.detach()}>
          popup 열기
        </button>
      )}
    </DetachablePanel>
  )
}

function FeatureMutationHarness() {
  const panelRef = useRef<DetachablePanelHandle>(null)
  const [featurePresent, setFeaturePresent] = useState(true)
  return (
    <DetachablePanel ref={panelRef} id="mutation-test" title="mutation test">
      {() => (
        <>
          <button type="button" onClick={() => panelRef.current?.detach()}>
            open mutation popup
          </button>
          {featurePresent ? (
            <button type="button" onClick={() => setFeaturePresent(false)}>
              delete feature
            </button>
          ) : (
            <span>feature deleted</span>
          )}
        </>
      )}
    </DetachablePanel>
  )
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('DetachablePanel keyboard relay', () => {
  it('relays one N event from a detached window to the canonical app window', () => {
    const { popup } = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onKeyDown = vi.fn()
    window.addEventListener('keydown', onKeyDown)
    try {
      render(<Harness />)
      fireEvent.click(screen.getByRole('button', { name: 'popup 열기' }))

      popup.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'n',
          code: 'KeyN',
          bubbles: true,
          cancelable: true,
        }),
      )

      expect(onKeyDown).toHaveBeenCalledOnce()
      expect(onKeyDown.mock.calls[0][0]).toMatchObject({ code: 'KeyN' })
    } finally {
      window.removeEventListener('keydown', onKeyDown)
    }
  })

  it('reconciles a force-closed popup and reopens it with one click', () => {
    vi.useFakeTimers()
    const first = fakePopup()
    const second = fakePopup()
    const open = vi
      .spyOn(window, 'open')
      .mockReturnValueOnce(first.popup)
      .mockReturnValueOnce(second.popup)
    const onDetachedChange = vi.fn()
    render(<Harness onDetachedChange={onDetachedChange} />)

    fireEvent.click(screen.getByRole('button', { name: 'popup 열기' }))
    expect(screen.getByLabelText('relay test 분리됨')).toBeInTheDocument()
    expect(onDetachedChange).toHaveBeenLastCalledWith(true)

    first.forceClose()
    act(() => vi.advanceTimersByTime(250))

    expect(screen.queryByLabelText('relay test 분리됨')).not.toBeInTheDocument()
    expect(onDetachedChange.mock.calls.map(([detached]) => detached)).toEqual([true, false])

    fireEvent.click(screen.getByRole('button', { name: 'popup 열기' }))
    expect(open).toHaveBeenCalledTimes(2)
    expect(screen.getByLabelText('relay test 분리됨')).toBeInTheDocument()
  })

  it('handles overlapping lifecycle close events only once', () => {
    const { popup } = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onDetachedChange = vi.fn()
    render(<Harness onDetachedChange={onDetachedChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'popup 열기' }))

    act(() => {
      popup.dispatchEvent(new Event('pagehide'))
      popup.dispatchEvent(new Event('unload'))
      popup.dispatchEvent(new Event('beforeunload'))
    })

    expect(onDetachedChange.mock.calls.map(([detached]) => detached)).toEqual([true, false])
    expect(screen.queryByLabelText('relay test 분리됨')).not.toBeInTheDocument()
  })

  it('keeps A/D relay active when deleting a feature control replaces popup content', () => {
    const { popup, addEventListener } = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onKeyDown = vi.fn((event: KeyboardEvent) => event.preventDefault())
    window.addEventListener('keydown', onKeyDown)
    try {
      render(<FeatureMutationHarness />)
      fireEvent.click(screen.getByRole('button', { name: 'open mutation popup' }))
      expect(addEventListener).toHaveBeenCalledWith('keydown', expect.any(Function), true)
      const deleteButton = Array.from(popup.document.querySelectorAll('button')).find(
        (button) => button.textContent?.trim() === 'delete feature',
      )
      expect(deleteButton).toBeDefined()
      act(() => deleteButton!.click())
      expect(popup.document.body.textContent).toContain('feature deleted')

      const shortcuts = [
        ['a', 'KeyA'],
        ['ArrowLeft', 'ArrowLeft'],
        ['d', 'KeyD'],
        ['ArrowRight', 'ArrowRight'],
        ['n', 'KeyN'],
        ['p', 'KeyP'],
        ['b', 'KeyB'],
        ['r', 'KeyR'],
        ['m', 'KeyM'],
        ['Enter', 'Enter'],
        ['Escape', 'Escape'],
      ] as const
      shortcuts.forEach(([key, code]) => {
        const shortcut = new KeyboardEvent('keydown', {
          key,
          code,
          bubbles: true,
          cancelable: true,
        })
        popup.dispatchEvent(shortcut)
        expect(shortcut.defaultPrevented).toBe(true)
      })

      expect(onKeyDown.mock.calls.map(([event]) => event.code)).toEqual(
        shortcuts.map(([, code]) => code),
      )

      ;['KeyJ', 'KeyK', 'KeyQ', 'KeyX', 'KeyF'].forEach((code) => {
        const retiredReviewShortcut = new KeyboardEvent('keydown', {
          key: code.slice(3).toLowerCase(),
          code,
          bubbles: true,
          cancelable: true,
        })
        popup.dispatchEvent(retiredReviewShortcut)
        expect(retiredReviewShortcut.defaultPrevented).toBe(false)
      })
      expect(onKeyDown.mock.calls.map(([event]) => event.code)).toEqual(
        shortcuts.map(([, code]) => code),
      )

      const shiftEnter = new KeyboardEvent('keydown', {
        key: 'Enter',
        code: 'Enter',
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      })
      popup.dispatchEvent(shiftEnter)
      expect(shiftEnter.defaultPrevented).toBe(true)
      expect(onKeyDown).toHaveBeenLastCalledWith(
        expect.objectContaining({ code: 'Enter', shiftKey: true }),
      )
    } finally {
      window.removeEventListener('keydown', onKeyDown)
    }
  })

  it('does not relay workspace shortcuts from popup form and action controls', () => {
    const { popup, addEventListener } = fakePopup()
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onKeyDown = vi.fn()
    window.addEventListener('keydown', onKeyDown)
    try {
      render(<Harness />)
      fireEvent.click(screen.getByRole('button', { name: 'popup 열기' }))
      const input = popup.document.createElement('input')
      const button = popup.document.createElement('button')
      const dialog = popup.document.createElement('section')
      dialog.setAttribute('role', 'dialog')
      popup.document.body.appendChild(input)
      popup.document.body.appendChild(button)
      popup.document.body.appendChild(dialog)
      const relay = addEventListener.mock.calls.find(([type]) => type === 'keydown')?.[1] as
        | ((event: KeyboardEvent) => void)
        | undefined
      expect(relay).toBeDefined()

      ;['KeyB', 'KeyR', 'KeyM', 'Enter', 'Escape'].forEach((code) => {
        const key = code.startsWith('Key') ? code.slice(3).toLowerCase() : code
        const event = new KeyboardEvent('keydown', {
          key,
          code,
          bubbles: true,
          cancelable: true,
        })
        Object.defineProperty(event, 'target', { value: input })
        relay?.(event)
      })

      const buttonEnter = new KeyboardEvent('keydown', {
        key: 'Enter',
        code: 'Enter',
        bubbles: true,
        cancelable: true,
      })
      Object.defineProperty(buttonEnter, 'target', { value: button })
      relay?.(buttonEnter)

      const dialogEscape = new KeyboardEvent('keydown', {
        key: 'Escape',
        code: 'Escape',
        bubbles: true,
        cancelable: true,
      })
      Object.defineProperty(dialogEscape, 'target', { value: dialog })
      relay?.(dialogEscape)

      expect(onKeyDown).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', onKeyDown)
    }
  })
})
