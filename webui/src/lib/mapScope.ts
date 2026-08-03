export interface MapTrackScope {
  showAllTracks: boolean
  effectiveTrackId: string | undefined
}

/**
 * An empty selected track is the UI's explicit "전체 구간" value. Keep that
 * distinct from an omitted value, where falling back to the current frame is
 * still useful for older callers and loading states.
 */
export function resolveMapTrackScope(
  selectedTrackId: string | undefined,
  selectedFrameTrackId: string | undefined,
  firstRouteTrackId: string | undefined,
  alwaysShowAllTracks: boolean,
): MapTrackScope {
  const showAllTracks = alwaysShowAllTracks || selectedTrackId === ''
  return {
    showAllTracks,
    effectiveTrackId: showAllTracks
      ? undefined
      : selectedTrackId || selectedFrameTrackId || firstRouteTrackId,
  }
}
