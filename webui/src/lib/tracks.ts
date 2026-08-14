import type { Track } from '../types'

const TRACK_NAME_COLLATOR = new Intl.Collator('ko-KR', {
  numeric: true,
  sensitivity: 'base',
})

function trackLabel(track: Pick<Track, 'id' | 'name'>): string {
  return track.name.trim() || track.id
}
/**
 * Human/numeric ordering for delivery track names. Case-only ties deliberately
 * retain the catalogue order so sorting stays stable across refreshes.
 */
export function naturalSortTracks<T extends Pick<Track, 'id' | 'name'>>(
  tracks: readonly T[],
): T[] {
  return tracks
    .map((track, index) => ({ track, index }))
    .sort(
      (left, right) =>
        TRACK_NAME_COLLATOR.compare(trackLabel(left.track), trackLabel(right.track)) ||
        left.index - right.index,
    )
    .map(({ track }) => track)
}
