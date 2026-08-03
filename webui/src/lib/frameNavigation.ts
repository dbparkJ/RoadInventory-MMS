export type FrameNavigationDirection = -1 | 1

type NavigationKeyboardEvent = Pick<
  KeyboardEvent,
  'altKey' | 'code' | 'ctrlKey' | 'defaultPrevented' | 'key' | 'metaKey' | 'shiftKey' | 'target'
>

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

export function frameNavigationDirection(
  event: NavigationKeyboardEvent,
): FrameNavigationDirection | null {
  if (
    event.defaultPrevented ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    isTextEntryTarget(event.target)
  ) {
    return null
  }

  if (event.key === 'ArrowLeft' || event.code === 'KeyA') return -1
  if (event.key === 'ArrowRight' || event.code === 'KeyD') return 1
  return null
}
