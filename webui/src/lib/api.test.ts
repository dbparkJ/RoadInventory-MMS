import { describe, expect, it } from 'vitest'
import { errorMessageFromPayload } from './api'

describe('errorMessageFromPayload', () => {
  it('renders FastAPI validation arrays as actionable field messages', () => {
    expect(
      errorMessageFromPayload(
        {
          detail: [
            { loc: ['body', 'parameters', 'confidence'], msg: 'Input should be less than or equal to 1' },
            { loc: ['body', 'track_ids'], msg: 'Field required' },
          ],
        },
        'fallback',
      ),
    ).toBe(
      'parameters.confidence: Input should be less than or equal to 1 · track_ids: Field required',
    )
  })

  it('keeps the server message ahead of nested details', () => {
    expect(
      errorMessageFromPayload({ message: '업로드 세션이 만료되었습니다.', detail: 'ignored' }, 'fallback'),
    ).toBe('업로드 세션이 만료되었습니다.')
  })
})
