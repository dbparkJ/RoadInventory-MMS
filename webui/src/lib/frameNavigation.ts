export type FrameNavigationDirection = -1 | 1

type NavigationKeyboardEvent = Pick<
  KeyboardEvent,
  'altKey' | 'code' | 'ctrlKey' | 'defaultPrevented' | 'key' | 'metaKey' | 'shiftKey' | 'target'
>

function currentDocument(): Document | null {
  return typeof document === 'undefined' ? null : document
}

function isRendered(element: Element, ownerDocument: Document): boolean {
  const ownerWindow = ownerDocument.defaultView
  let current: Element | null = element
  while (current) {
    if (
      current.hasAttribute('hidden') ||
      current.getAttribute('aria-hidden')?.trim().toLowerCase() === 'true'
    ) {
      return false
    }
    const style = ownerWindow?.getComputedStyle(current)
    if (
      style?.display === 'none' ||
      style?.visibility === 'hidden' ||
      style?.visibility === 'collapse'
    ) {
      return false
    }
    current = current.parentElement
  }
  return true
}

/**
 * Return whether this document currently contains a visible modal dialog.
 *
 * Workspace shortcuts are registered on `window` in capture phase so a modal
 * cannot stop them from a descendant key handler. Every global shortcut must
 * therefore consult this guard before changing the workspace behind a dialog.
 */
export function hasOpenModalDialog(
  ownerDocument: Document | null = currentDocument(),
): boolean {
  if (!ownerDocument) return false
  return [...ownerDocument.querySelectorAll('[role="dialog"][aria-modal="true"]')]
    .some((dialog) => isRendered(dialog, ownerDocument))
}

export function isTextEntryTarget(target: EventTarget | null): boolean {
  if (!target || typeof target !== 'object') return false

  const candidate = target as {
    isContentEditable?: boolean
    tagName?: string
    closest?: (selector: string) => Element | null
  }
  if (candidate.isContentEditable) return true
  if (candidate.tagName && ['INPUT', 'SELECT', 'TEXTAREA'].includes(candidate.tagName)) return true

  const editableAncestor = candidate.closest?.('[contenteditable], [role="textbox"]')
  return Boolean(
    editableAncestor && editableAncestor.getAttribute('contenteditable')?.toLowerCase() !== 'false',
  )
}

/** Prevent global workspace keys from also activating focused UI controls. */
export function isWorkspaceShortcutBlockedTarget(target: EventTarget | null): boolean {
  if (isTextEntryTarget(target)) return true
  if (!target || typeof target !== 'object') return false
  const candidate = target as {
    tagName?: string
    closest?: (selector: string) => Element | null
  }
  if (candidate.tagName && ['BUTTON', 'A', 'SUMMARY'].includes(candidate.tagName.toUpperCase())) {
    return true
  }
  return Boolean(candidate.closest?.(
    '[role="dialog"], button, a[href], summary, [role="button"], [role="menuitem"], [role="option"], [role="switch"], [role="checkbox"], [role="radio"]',
  ))
}

export function frameNavigationDirection(
  event: NavigationKeyboardEvent,
): FrameNavigationDirection | null {
  if (
    event.defaultPrevented ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    isWorkspaceShortcutBlockedTarget(event.target)
  ) {
    return null
  }

  if (event.key === 'ArrowLeft' || event.code === 'KeyA') return -1
  if (event.key === 'ArrowRight' || event.code === 'KeyD') return 1
  return null
}
