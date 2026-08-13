import { describe, expect, it } from 'vitest'
import { resolveMapTrackScope } from './mapScope'

describe('resolveMapTrackScope', () => {
  it('shows every track when the 전체 구간 sentinel is selected', () => {
    expect(resolveMapTrackScope('', 'track-1', 'track-1', false)).toEqual({
      showAllTracks: true,
      effectiveTrackId: undefined,
    })
  })

  it('keeps an explicitly selected track active', () => {
    expect(resolveMapTrackScope('track-2', 'track-1', 'track-1', false)).toEqual({
      showAllTracks: false,
      effectiveTrackId: 'track-2',
    })
  })

  it('honours the persistent all-track display setting', () => {
    expect(resolveMapTrackScope(undefined, 'track-2', 'track-1', true)).toEqual({
      showAllTracks: true,
      effectiveTrackId: undefined,
    })
  })

  it('keeps an explicit track isolated even when the all-track preference was saved', () => {
    expect(resolveMapTrackScope('track-2', 'track-1', 'track-1', true)).toEqual({
      showAllTracks: false,
      effectiveTrackId: 'track-2',
    })
  })
})
