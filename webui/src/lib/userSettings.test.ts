import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  DEFAULT_USER_SETTINGS,
  readUserSettings,
  sanitizeUserSettings,
  USER_SETTINGS_STORAGE_KEY,
  useUserSettings,
} from './userSettings'

describe('user settings', () => {
  beforeEach(() => window.localStorage.clear())

  it('falls back field-by-field and clamps unsafe numeric values', () => {
    expect(
      sanitizeUserSettings({
        panoramaForwardOffsetDeg: 720,
        panoramaPointOverlayEnabled: true,
        panoramaPointOverlayOpacity: -3,
        panoramaDefaultQuality: 'original',
        detectionVisibilityDistanceM: 900,
        showAllMapTracks: 'yes',
      }),
    ).toEqual({
      panoramaForwardOffsetDeg: 180,
      panoramaPointOverlayEnabled: true,
      panoramaImageOpacity: 0,
      panoramaDefaultQuality: 'high',
      detectionVisibilityDistanceM: 200,
      showAllMapTracks: false,
    })
  })

  it('migrates the previous point-opacity value to panorama image opacity', () => {
    expect(sanitizeUserSettings({ panoramaPointOverlayOpacity: 0.4 }).panoramaImageOpacity).toBe(0.4)
    expect(
      sanitizeUserSettings({ panoramaPointOverlayOpacity: 0.4, panoramaImageOpacity: 0.8 })
        .panoramaImageOpacity,
    ).toBe(0.8)
  })

  it('recovers from damaged local storage and defaults to the active track only', () => {
    window.localStorage.setItem(USER_SETTINGS_STORAGE_KEY, '{not-json')

    expect(readUserSettings()).toEqual(DEFAULT_USER_SETTINGS)
    expect(readUserSettings().showAllMapTracks).toBe(false)
  })

  it('persists updates and resets all values', () => {
    const { result } = renderHook(() => useUserSettings())

    act(() => {
      result.current.updateSettings({
        panoramaForwardOffsetDeg: -12.5,
        panoramaPointOverlayEnabled: true,
        showAllMapTracks: true,
      })
    })

    expect(JSON.parse(window.localStorage.getItem(USER_SETTINGS_STORAGE_KEY) ?? '{}')).toEqual(
      expect.objectContaining({
        panoramaForwardOffsetDeg: -12.5,
        panoramaPointOverlayEnabled: true,
        showAllMapTracks: true,
      }),
    )

    act(() => result.current.resetSettings())
    expect(result.current.settings).toEqual(DEFAULT_USER_SETTINGS)
  })
})
