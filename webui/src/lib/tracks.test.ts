import { describe, expect, it } from 'vitest'
import { naturalSortTracks } from './tracks'

describe('naturalSortTracks', () => {
  it('sorts numeric track names naturally', () => {
    expect(
      naturalSortTracks([
        { id: '10', name: 'SEC_10' },
        { id: '2', name: 'SEC_02' },
        { id: '5', name: 'SEC_05' },
        { id: '1', name: 'SEC_01' },
      ]).map((track) => track.name),
    ).toEqual(['SEC_01', 'SEC_02', 'SEC_05', 'SEC_10'])
  })

  it('keeps catalogue order for case-only ties and falls back to the id', () => {
    expect(
      naturalSortTracks([
        { id: 'second', name: 'sec_01' },
        { id: 'first', name: 'SEC_01' },
        { id: 'SEC_02', name: '   ' },
      ]).map((track) => track.id),
    ).toEqual(['second', 'first', 'SEC_02'])
  })
})
