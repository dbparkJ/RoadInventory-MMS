import { useCallback, useEffect, useState } from 'react'

export type PanoramaQuality = 'fast' | 'high' | 'ultra'

export interface UserSettings {
  panoramaForwardOffsetDeg: number
  panoramaPointOverlayEnabled: boolean
  panoramaImageOpacity: number
  panoramaDefaultQuality: PanoramaQuality
  detectionVisibilityDistanceM: number
  showAllMapTracks: boolean
}

export type UserSettingsPatch = Partial<UserSettings>

export const USER_SETTINGS_STORAGE_KEY = 'mms-operator-console:user-settings:v1'

export const DEFAULT_USER_SETTINGS: Readonly<UserSettings> = Object.freeze({
  panoramaForwardOffsetDeg: 0,
  panoramaPointOverlayEnabled: false,
  // When points are overlaid, slightly fading the camera image keeps the
  // measurements legible without hiding either source.
  panoramaImageOpacity: 0.65,
  panoramaDefaultQuality: 'high',
  // Keep panorama detections local to the current vehicle pose so distant
  // objects do not pile up at the same viewing angle.
  detectionVisibilityDistanceM: 45,
  // A dense delivery can contain many overlapping routes. Keep only the
  // active track visible until an operator explicitly asks for all tracks.
  showAllMapTracks: false,
})

type ReadableStorage = Pick<Storage, 'getItem'>
type WritableStorage = Pick<Storage, 'setItem'>

function finiteNumber(value: unknown, fallback: number, minimum: number, maximum: number) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, value))
}

function isPanoramaQuality(value: unknown): value is PanoramaQuality {
  return value === 'fast' || value === 'high' || value === 'ultra'
}

export function sanitizeUserSettings(value: unknown): UserSettings {
  const candidate = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}

  return {
    panoramaForwardOffsetDeg: finiteNumber(
      candidate.panoramaForwardOffsetDeg,
      DEFAULT_USER_SETTINGS.panoramaForwardOffsetDeg,
      -180,
      180,
    ),
    panoramaPointOverlayEnabled:
      typeof candidate.panoramaPointOverlayEnabled === 'boolean'
        ? candidate.panoramaPointOverlayEnabled
        : DEFAULT_USER_SETTINGS.panoramaPointOverlayEnabled,
    panoramaImageOpacity: finiteNumber(
      candidate.panoramaImageOpacity ?? candidate.panoramaPointOverlayOpacity,
      DEFAULT_USER_SETTINGS.panoramaImageOpacity,
      0,
      1,
    ),
    panoramaDefaultQuality: isPanoramaQuality(candidate.panoramaDefaultQuality)
      ? candidate.panoramaDefaultQuality
      : DEFAULT_USER_SETTINGS.panoramaDefaultQuality,
    detectionVisibilityDistanceM: finiteNumber(
      candidate.detectionVisibilityDistanceM,
      DEFAULT_USER_SETTINGS.detectionVisibilityDistanceM,
      5,
      200,
    ),
    showAllMapTracks:
      typeof candidate.showAllMapTracks === 'boolean'
        ? candidate.showAllMapTracks
        : DEFAULT_USER_SETTINGS.showAllMapTracks,
  }
}

function browserStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function readUserSettings(storage: ReadableStorage | null = browserStorage()): UserSettings {
  if (!storage) return { ...DEFAULT_USER_SETTINGS }
  try {
    const stored = storage.getItem(USER_SETTINGS_STORAGE_KEY)
    if (!stored) return { ...DEFAULT_USER_SETTINGS }
    return sanitizeUserSettings(JSON.parse(stored) as unknown)
  } catch {
    return { ...DEFAULT_USER_SETTINGS }
  }
}

export function writeUserSettings(
  settings: UserSettings,
  storage: WritableStorage | null = browserStorage(),
) {
  if (!storage) return
  try {
    storage.setItem(USER_SETTINGS_STORAGE_KEY, JSON.stringify(sanitizeUserSettings(settings)))
  } catch {
    // Browsing can continue with in-memory settings when storage is unavailable
    // (private browsing policy, quota, or a locked-down embedded browser).
  }
}

export function useUserSettings() {
  const [settings, setSettings] = useState<UserSettings>(() => readUserSettings())

  useEffect(() => {
    writeUserSettings(settings)
  }, [settings])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const synchronizeDetachedWindow = (event: StorageEvent) => {
      if (event.key !== USER_SETTINGS_STORAGE_KEY) return
      if (!event.newValue) {
        setSettings({ ...DEFAULT_USER_SETTINGS })
        return
      }
      try {
        setSettings(sanitizeUserSettings(JSON.parse(event.newValue) as unknown))
      } catch {
        setSettings({ ...DEFAULT_USER_SETTINGS })
      }
    }

    window.addEventListener('storage', synchronizeDetachedWindow)
    return () => window.removeEventListener('storage', synchronizeDetachedWindow)
  }, [])

  const updateSettings = useCallback((patch: UserSettingsPatch) => {
    setSettings((current) => sanitizeUserSettings({ ...current, ...patch }))
  }, [])

  const resetSettings = useCallback(() => {
    setSettings({ ...DEFAULT_USER_SETTINGS })
  }, [])

  return { settings, updateSettings, resetSettings }
}
