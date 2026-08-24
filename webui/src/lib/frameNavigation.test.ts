import { describe, expect, it } from 'vitest'
import {
  frameNavigationDirection,
  hasOpenModalDialog,
  isTextEntryTarget,
  isWorkspaceShortcutBlockedTarget,
} from './frameNavigation'

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

  it('blocks global shortcuts from focused interactive controls', () => {
    const button = document.createElement('button')
    const icon = document.createElement('span')
    button.appendChild(icon)
    const link = document.createElement('a')
    link.href = '#result'
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    const dialogContent = document.createElement('div')
    dialog.appendChild(dialogContent)

    expect(isWorkspaceShortcutBlockedTarget(button)).toBe(true)
    expect(isWorkspaceShortcutBlockedTarget(icon)).toBe(true)
    expect(isWorkspaceShortcutBlockedTarget(link)).toBe(true)
    expect(isWorkspaceShortcutBlockedTarget(dialog)).toBe(true)
    expect(isWorkspaceShortcutBlockedTarget(dialogContent)).toBe(true)
    expect(frameNavigationDirection(keyboardEvent({ key: 'ArrowRight', target: button }))).toBeNull()
    expect(isWorkspaceShortcutBlockedTarget(document.createElement('div'))).toBe(false)
  })

  it('detects only visible modal dialogs in the current document', () => {
    const host = document.createElement('div')
    const dialog = document.createElement('section')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    host.appendChild(dialog)
    document.body.appendChild(host)

    try {
      expect(hasOpenModalDialog()).toBe(true)

      dialog.hidden = true
      expect(hasOpenModalDialog()).toBe(false)

      dialog.hidden = false
      host.setAttribute('aria-hidden', 'true')
      expect(hasOpenModalDialog()).toBe(false)

      host.removeAttribute('aria-hidden')
      host.style.display = 'none'
      expect(hasOpenModalDialog()).toBe(false)

      dialog.setAttribute('aria-modal', 'false')
      expect(hasOpenModalDialog()).toBe(false)
    } finally {
      host.remove()
    }
  })
})
