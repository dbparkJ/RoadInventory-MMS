import { describe, expect, it } from 'vitest'
import { frameNavigationDirection, isTextEntryTarget } from './frameNavigation'

function keyboardEvent(overrides: Partial<KeyboardEvent> = {}) {
  return {
    altKey: false,
    code: '',
    ctrlKey: false,
    defaultPrevented: false,
    key: '',
    metaKey: false,
    shiftKey: false,
    target: null,
    ...overrides,
  } as KeyboardEvent
}

describe('frame navigation shortcuts', () => {
  it('maps arrow and A/D keys to frame directions', () => {
    expect(frameNavigationDirection(keyboardEvent({ key: 'ArrowLeft' }))).toBe(-1)
    expect(frameNavigationDirection(keyboardEvent({ code: 'KeyA' }))).toBe(-1)
    expect(frameNavigationDirection(keyboardEvent({ key: 'ArrowRight' }))).toBe(1)
    expect(frameNavigationDirection(keyboardEvent({ code: 'KeyD' }))).toBe(1)
  })

  it('does not interfere with text entry or modified shortcuts', () => {
    const input = document.createElement('input')
    const editor = document.createElement('div')
    editor.setAttribute('contenteditable', 'true')
    const editorChild = document.createElement('span')
    editor.appendChild(editorChild)

    expect(isTextEntryTarget(input)).toBe(true)
    expect(isTextEntryTarget(editorChild)).toBe(true)
    expect(frameNavigationDirection(keyboardEvent({ key: 'ArrowRight', target: input }))).toBeNull()
    expect(frameNavigationDirection(keyboardEvent({ key: 'ArrowRight', ctrlKey: true }))).toBeNull()
  })
})
