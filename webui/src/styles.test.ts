import { afterEach, describe, expect, it } from 'vitest'
import './styles.css'
import stylesheet from './styles.css?raw'

afterEach(() => document.body.replaceChildren())

describe('detached panel visibility', () => {
  it.each(['detachable-host', 'settings-layer', 'utility-layer'])(
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

describe('map controls responsive layout', () => {
  it('uses the actual map viewport width to keep tools clear of expanded and collapsed layer cards', () => {
    expect(stylesheet).toContain('container-name: map-viewport;')
    expect(stylesheet).toContain('container-type: inline-size;')
    expect(stylesheet).toContain('@container map-viewport (max-width: 760px)')
    expect(stylesheet).toContain('@container map-viewport (max-width: 520px)')
    expect(stylesheet).toContain('width: min(282px, calc(100% - 133px));')
    expect(stylesheet).toContain('width: min(210px, calc(100% - 133px));')
  })
})
