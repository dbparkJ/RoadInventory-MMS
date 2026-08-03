import { afterEach, describe, expect, it } from 'vitest'
import './styles.css'

afterEach(() => document.body.replaceChildren())

describe('detached panel visibility', () => {
  it.each(['detachable-host', 'settings-layer'])(
    'keeps a hidden %s out of layout despite its flex display rule',
    (className) => {
      const element = document.createElement('div')
      element.className = className
      element.hidden = true
      document.body.appendChild(element)

      expect(window.getComputedStyle(element).display).toBe('none')
    },
  )
})
