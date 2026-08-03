import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api } from '../lib/api'
import { isTextEntryTarget } from '../lib/frameNavigation'
import type {
  OverlayCoordinateSpace,
  OverlayEncoding,
  OverlayFeature,
  OverlayFeatureCollection,
  OverlayLayer,
} from '../types'

const LAYER_COLORS = ['#2bcfa8', '#ffb84d', '#65a9ff', '#ff6f91', '#b38cff', '#f4e04d']
const FEATURE_PAGE_SIZE = 3_000

interface LayerFeatures {
  wgs84: OverlayFeatureCollection | null
  dataset: OverlayFeatureCollection | null
  loading: boolean
  loadingWgs84?: boolean
  loadingDataset?: boolean
  error?: string
  errorWgs84?: string
  errorDataset?: string
}

export interface OverlaySelection {
  layerId: string
  featureId: string | number
}

interface OverlayContextValue {
  datasetId: string
  layers: OverlayLayer[]
  features: Record<string, LayerFeatures>
  visibleLayerIds: Set<string>
  selected: OverlaySelection | null
  selectedLayer: OverlayLayer | null
  selectedFeature: OverlayFeature | null
  selectedDatasetFeature: OverlayFeature | null
  mapFeatures: OverlayFeature[]
  datasetFeatures: Array<{ layerId: string; color: string; feature: OverlayFeature }>
  loading: boolean
  uploading: boolean
  pickMode: boolean
  setPickMode: (enabled: boolean) => void
  refresh: () => Promise<void>
  ensureDatasetFeatures: (layerId: string) => Promise<void>
  loadMoreDatasetFeatures: (layerId: string) => Promise<void>
  upload: (files: File[], name?: string, crs?: string, encoding?: OverlayEncoding) => Promise<void>
  removeLayer: (layerId: string) => Promise<void>
  toggleLayer: (layerId: string) => void
  selectFeature: (selection: OverlaySelection | null) => void
  updateSelected: (patch: {
    geometry?: { type: 'Point'; coordinates: [number, number, number?] }
    coordinate_space?: OverlayCoordinateSpace
    properties?: Record<string, unknown>
  }) => Promise<void>
  applyPickedCoordinate: (
    coordinates: [number, number, number?],
    coordinateSpace: OverlayCoordinateSpace,
  ) => Promise<void>
  deleteSelected: () => Promise<void>
  layerColor: (layerId: string) => string
}

const OverlayContext = createContext<OverlayContextValue | null>(null)

function featureId(feature: OverlayFeature): string {
  return String(feature.id)
}

function emptyCollection(revision = 0): OverlayFeatureCollection {
  return {
    type: 'FeatureCollection',
    features: [],
    fields: [],
    total: 0,
    offset: 0,
    limit: 0,
    revision,
  }
}

export function OverlayProvider({
  datasetId,
  demoMode,
  notify,
  children,
}: {
  datasetId: string
  demoMode: boolean
  notify?: (entry: { tone: 'success' | 'error' | 'info'; title: string; message?: string }) => void
  children: ReactNode
}) {
  const [layers, setLayers] = useState<OverlayLayer[]>([])
  const [features, setFeatures] = useState<Record<string, LayerFeatures>>({})
  const [visibleLayerIds, setVisibleLayerIds] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<OverlaySelection | null>(null)
  const [selectedDatasetDetail, setSelectedDatasetDetail] = useState<{
    layerId: string
    featureId: string | number
    feature: OverlayFeature
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [pickMode, setPickMode] = useState(false)
  const requestGeneration = useRef(0)
  const refreshControllerRef = useRef<AbortController | null>(null)
  const loadedDatasetRef = useRef('')
  const knownLayerIdsRef = useRef<Set<string>>(new Set())
  const coordinateLoadsRef = useRef<Set<string>>(new Set())
  const selectionRequestRef = useRef(0)
  const visibleLayerIdsRef = useRef(visibleLayerIds)
  visibleLayerIdsRef.current = visibleLayerIds

  const layerColor = useCallback((layerId: string) => {
    let hash = 0
    for (let index = 0; index < layerId.length; index += 1) {
      hash = (hash * 31 + layerId.charCodeAt(index)) | 0
    }
    return LAYER_COLORS[Math.abs(hash) % LAYER_COLORS.length]
  }, [])

  const loadFeaturePage = useCallback(
    async (
      layer: OverlayLayer,
      coordinateSpace: OverlayCoordinateSpace,
      generation: number,
      offset = 0,
      append = false,
      signal?: AbortSignal,
    ) => {
      const loadKey = `${generation}:${layer.id}:${coordinateSpace}:${offset}`
      if (coordinateLoadsRef.current.has(loadKey)) return
      coordinateLoadsRef.current.add(loadKey)
      setFeatures((current) => ({
        ...current,
        [layer.id]: {
          ...(current[layer.id] ?? { wgs84: null, dataset: null }),
          loading: true,
          [coordinateSpace === 'wgs84' ? 'loadingWgs84' : 'loadingDataset']: true,
          error: undefined,
        },
      }))
      try {
        const page = await api.overlayFeatures(
          datasetId,
          layer.id,
          coordinateSpace,
          offset,
          FEATURE_PAGE_SIZE,
          signal,
        )
        if (requestGeneration.current !== generation || signal?.aborted) return
        setFeatures((current) => {
          const currentLayer = current[layer.id] ?? { wgs84: null, dataset: null, loading: false }
          const existingCollection = currentLayer[coordinateSpace]
          let nextCollection = page
          if (append && existingCollection) {
            const existingIds = new Set(existingCollection.features.map((feature) => String(feature.id)))
            nextCollection = {
              ...page,
              offset: 0,
              features: [
                ...existingCollection.features,
                ...page.features.filter((candidate) => !existingIds.has(String(candidate.id))),
              ],
            }
          }
          return {
            ...current,
            [layer.id]: {
              ...currentLayer,
              [coordinateSpace]: nextCollection,
              [coordinateSpace === 'wgs84' ? 'loadingWgs84' : 'loadingDataset']: false,
              loading: Boolean(
                currentLayer[
                  coordinateSpace === 'wgs84' ? 'loadingDataset' : 'loadingWgs84'
                ],
              ),
              [coordinateSpace === 'wgs84' ? 'errorWgs84' : 'errorDataset']: undefined,
              error:
                currentLayer[
                  coordinateSpace === 'wgs84' ? 'errorDataset' : 'errorWgs84'
                ],
            },
          }
        })
        setLayers((current) => {
          let changed = false
          const next = current.map((candidate) => {
            if (
              candidate.id !== layer.id ||
              (candidate.feature_count === page.total && candidate.revision === page.revision)
            ) {
              return candidate
            }
            changed = true
            return { ...candidate, feature_count: page.total, revision: page.revision }
          })
          return changed ? next : current
        })
      } catch (reason) {
        if (signal?.aborted || requestGeneration.current !== generation) return
        setFeatures((current) => {
          const message = reason instanceof Error ? reason.message : 'SHP 피처를 불러오지 못했습니다.'
          return {
            ...current,
            [layer.id]: {
              ...(current[layer.id] ?? {
              wgs84: coordinateSpace === 'wgs84' ? emptyCollection(layer.revision) : null,
              dataset: coordinateSpace === 'dataset' ? emptyCollection(layer.revision) : null,
              }),
              [coordinateSpace === 'wgs84' ? 'loadingWgs84' : 'loadingDataset']: false,
              [coordinateSpace === 'wgs84' ? 'errorWgs84' : 'errorDataset']: message,
              loading: Boolean(
                current[layer.id]?.[
                  coordinateSpace === 'wgs84' ? 'loadingDataset' : 'loadingWgs84'
                ],
              ),
              error: message,
            },
          }
        })
      } finally {
        coordinateLoadsRef.current.delete(loadKey)
      }
    },
    [datasetId],
  )

  const refresh = useCallback(async () => {
    const generation = requestGeneration.current + 1
    requestGeneration.current = generation
    refreshControllerRef.current?.abort()
    if (!datasetId || demoMode) {
      loadedDatasetRef.current = datasetId
      knownLayerIdsRef.current = new Set()
      setLayers([])
      setFeatures({})
      setVisibleLayerIds(new Set())
      setSelected(null)
      setSelectedDatasetDetail(null)
      return
    }
    const controller = new AbortController()
    refreshControllerRef.current = controller
    if (loadedDatasetRef.current !== datasetId) {
      loadedDatasetRef.current = datasetId
      knownLayerIdsRef.current = new Set()
      setLayers([])
      setFeatures({})
      setVisibleLayerIds(new Set())
      setSelected(null)
      setSelectedDatasetDetail(null)
    }
    setLoading(true)
    try {
      const response = await api.overlays(datasetId, controller.signal)
      if (requestGeneration.current !== generation) return
      const nextLayers = response.items ?? []
      const previousLayerIds = knownLayerIdsRef.current
      const nextLayerIds = new Set(nextLayers.map((layer) => layer.id))
      const visibleForLoad = new Set(
        [...visibleLayerIdsRef.current].filter((layerId) => nextLayerIds.has(layerId)),
      )
      nextLayers.forEach((layer) => {
        if (!previousLayerIds.has(layer.id)) visibleForLoad.add(layer.id)
      })
      knownLayerIdsRef.current = nextLayerIds
      setLayers(nextLayers)
      setFeatures((current) =>
        Object.fromEntries(
          nextLayers.map((layer) => {
            const cached = current[layer.id]
            const cachedRevision = cached?.dataset?.revision ?? cached?.wgs84?.revision
            return [
              layer.id,
              cached && cachedRevision === layer.revision
                ? cached
                : { wgs84: null, dataset: null, loading: false },
            ]
          }),
        ),
      )
      setVisibleLayerIds(visibleForLoad)
      setSelected((current) =>
        current && nextLayers.some((layer) => layer.id === current.layerId) ? current : null,
      )
      const visibleLayers = nextLayers.filter((layer) => visibleForLoad.has(layer.id))
      let nextLayerIndex = 0
      const loadVisibleMapPages = async () => {
        while (!controller.signal.aborted) {
          const layer = visibleLayers[nextLayerIndex]
          nextLayerIndex += 1
          if (!layer) return
          await loadFeaturePage(layer, 'wgs84', generation, 0, false, controller.signal)
        }
      }
      await Promise.all(
        Array.from({ length: Math.min(4, visibleLayers.length) }, () => loadVisibleMapPages()),
      )
    } catch (reason) {
      if (requestGeneration.current !== generation) return
      notify?.({
        tone: 'error',
        title: 'SHP 레이어를 불러오지 못했습니다',
        message: reason instanceof Error ? reason.message : undefined,
      })
    } finally {
      if (requestGeneration.current === generation) setLoading(false)
      if (refreshControllerRef.current === controller) refreshControllerRef.current = null
    }
  }, [datasetId, demoMode, loadFeaturePage, notify])

  useEffect(() => {
    void refresh()
    const onChanged = () => void refresh()
    window.addEventListener('mms-overlay-changed', onChanged)
    return () => {
      window.removeEventListener('mms-overlay-changed', onChanged)
      refreshControllerRef.current?.abort()
    }
  }, [refresh])

  const ensureDatasetFeatures = useCallback(
    async (layerId: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!layer || features[layerId]?.dataset || features[layerId]?.loadingDataset) return
      await loadFeaturePage(layer, 'dataset', requestGeneration.current)
    },
    [features, layers, loadFeaturePage],
  )

  const loadMoreDatasetFeatures = useCallback(
    async (layerId: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      const collection = features[layerId]?.dataset
      if (!layer || !collection || features[layerId]?.loadingDataset) return
      const wgs84 = features[layerId]?.wgs84
      const datasetOffset = collection.next_offset ?? collection.features.length
      const wgs84Offset = wgs84?.next_offset ?? wgs84?.features.length ?? 0
      await Promise.all([
        ...(datasetOffset < collection.total
          ? [
              loadFeaturePage(
                layer,
                'dataset',
                requestGeneration.current,
                datasetOffset,
                true,
              ),
            ]
          : []),
        ...(wgs84 && wgs84Offset < wgs84.total
          ? [
              loadFeaturePage(
                layer,
                'wgs84',
                requestGeneration.current,
                wgs84Offset,
                true,
              ),
            ]
          : []),
      ])
    },
    [features, layers, loadFeaturePage],
  )

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        isTextEntryTarget(event.target)
      ) {
        return
      }
      if (event.key === 'Escape' && pickMode) {
        event.preventDefault()
        setPickMode(false)
      } else if (event.code === 'KeyP' && selected) {
        event.preventDefault()
        setPickMode((current) => !current)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [pickMode, selected])

  const upload = useCallback(
    async (files: File[], name?: string, crs?: string, encoding: OverlayEncoding = 'auto') => {
      if (!datasetId || !files.length) return
      if (demoMode) {
        notify?.({ tone: 'info', title: '데모 모드에서는 SHP를 등록할 수 없습니다.' })
        return
      }
      setUploading(true)
      try {
        const response = await api.uploadOverlay(datasetId, files, name, crs, encoding)
        notify?.({
          tone: 'success',
          title: 'SHP 레이어를 등록했습니다',
          message: `${response.layer.name} · ${response.layer.feature_count.toLocaleString('ko-KR')}개 피처`,
        })
        await refresh()
        setSelected(null)
      } catch (reason) {
        notify?.({
          tone: 'error',
          title: 'SHP 레이어를 등록하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      } finally {
        setUploading(false)
      }
    },
    [datasetId, demoMode, notify, refresh],
  )

  const removeLayer = useCallback(
    async (layerId: string) => {
      if (!datasetId || demoMode) return
      await api.deleteOverlay(datasetId, layerId)
      if (selected?.layerId === layerId) setSelected(null)
      notify?.({ tone: 'info', title: 'SHP 레이어 등록을 제거했습니다', message: '업로드 원본은 보존됩니다.' })
      await refresh()
    },
    [datasetId, demoMode, notify, refresh, selected?.layerId],
  )

  const toggleLayer = useCallback(
    (layerId: string) => {
      const becomingVisible = !visibleLayerIds.has(layerId)
      setVisibleLayerIds((current) => {
        const next = new Set(current)
        if (next.has(layerId)) next.delete(layerId)
        else next.add(layerId)
        return next
      })
      if (becomingVisible) {
        const layer = layers.find((candidate) => candidate.id === layerId)
        if (layer && !features[layerId]?.wgs84 && !features[layerId]?.loadingWgs84) {
          void loadFeaturePage(layer, 'wgs84', requestGeneration.current)
        }
      }
    },
    [features, layers, loadFeaturePage, visibleLayerIds],
  )

  const selectFeature = useCallback(
    (selection: OverlaySelection | null) => {
      setSelected(selection)
      if (!selection) {
        selectionRequestRef.current += 1
        setSelectedDatasetDetail(null)
        return
      }
      setSelectedDatasetDetail(null)
      if (selection) {
        window.dispatchEvent(
          new CustomEvent('mms-overlay-selected', { detail: { datasetId, selection } }),
        )
      }
    },
    [datasetId],
  )

  const selectedLayer = useMemo(
    () => layers.find((layer) => layer.id === selected?.layerId) ?? null,
    [layers, selected?.layerId],
  )

  useEffect(() => {
    if (!selected || !selectedLayer || !datasetId || demoMode) return
    const cached = features[selected.layerId]?.dataset?.features.find(
      (feature) => String(feature.id) === String(selected.featureId),
    )
    if (cached) {
      setSelectedDatasetDetail({ ...selected, feature: cached })
      return
    }
    const requestId = selectionRequestRef.current + 1
    selectionRequestRef.current = requestId
    const controller = new AbortController()
    void api
      .overlayFeature(datasetId, selected.layerId, selected.featureId, 'dataset', controller.signal)
      .then((response) => {
        if (selectionRequestRef.current !== requestId || controller.signal.aborted) return
        setSelectedDatasetDetail({ ...selected, feature: response.feature })
      })
      .catch((reason: unknown) => {
        if (selectionRequestRef.current !== requestId || controller.signal.aborted) return
        notify?.({
          tone: 'error',
          title: '선택한 SHP 피처를 불러오지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
      })
    return () => controller.abort()
    // The selected layer revision is the server-side invalidation token.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId, demoMode, selected, selectedLayer?.revision])
  const selectedFeature = useMemo(
    () =>
      selected
        ? features[selected.layerId]?.wgs84?.features.find(
            (feature) => featureId(feature) === String(selected.featureId),
          ) ?? null
        : null,
    [features, selected],
  )
  const selectedDatasetFeature = useMemo(
    () =>
      selected
        ? features[selected.layerId]?.dataset?.features.find(
            (feature) => featureId(feature) === String(selected.featureId),
          ) ??
          (selectedDatasetDetail?.layerId === selected.layerId &&
          String(selectedDatasetDetail.featureId) === String(selected.featureId)
            ? selectedDatasetDetail.feature
            : null)
        : null,
    [features, selected, selectedDatasetDetail],
  )

  const updateSelected = useCallback(
    async (patch: {
      geometry?: { type: 'Point'; coordinates: [number, number, number?] }
      coordinate_space?: OverlayCoordinateSpace
      properties?: Record<string, unknown>
    }) => {
      if (!datasetId || !selected || !selectedLayer || demoMode) return
      const currentRevision =
        features[selected.layerId]?.dataset?.revision ?? selectedLayer.revision
      try {
        const response = await api.patchOverlayFeature(datasetId, selected.layerId, selected.featureId, {
          ...patch,
          expected_revision: currentRevision,
        })
        if (response.coordinate_space === 'dataset') {
          setSelectedDatasetDetail({ ...selected, feature: response.feature })
        }
        const generation = requestGeneration.current
        const reloadDataset = Boolean(features[selected.layerId]?.dataset)
        await Promise.all([
          loadFeaturePage(selectedLayer, 'wgs84', generation),
          ...(reloadDataset ? [loadFeaturePage(selectedLayer, 'dataset', generation)] : []),
        ])
        notify?.({ tone: 'success', title: 'SHP 피처를 저장했습니다' })
      } catch (reason) {
        notify?.({
          tone: 'error',
          title: 'SHP 피처를 저장하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      }
    },
    [datasetId, demoMode, features, loadFeaturePage, notify, selected, selectedLayer],
  )

  const applyPickedCoordinate = useCallback(
    async (
      coordinates: [number, number, number?],
      coordinateSpace: OverlayCoordinateSpace,
    ) => {
      if (!selected) {
        notify?.({ tone: 'info', title: '먼저 속성표에서 수정할 피처를 선택해 주세요.' })
        return
      }
      try {
        await updateSelected({
          geometry: { type: 'Point', coordinates },
          coordinate_space: coordinateSpace,
        })
        setPickMode(false)
      } catch {
        // updateSelected already emitted the actionable server error.
      }
    },
    [notify, selected, updateSelected],
  )

  const deleteSelected = useCallback(async () => {
    if (!datasetId || !selected || !selectedLayer || demoMode) return
    const currentRevision = features[selected.layerId]?.dataset?.revision ?? selectedLayer.revision
    await api.deleteOverlayFeature(datasetId, selected.layerId, selected.featureId, currentRevision)
    setSelected(null)
    setSelectedDatasetDetail(null)
    notify?.({ tone: 'info', title: '선택한 SHP 피처를 삭제했습니다' })
    const generation = requestGeneration.current
    const reloadDataset = Boolean(features[selected.layerId]?.dataset)
    await Promise.all([
      loadFeaturePage(selectedLayer, 'wgs84', generation),
      ...(reloadDataset ? [loadFeaturePage(selectedLayer, 'dataset', generation)] : []),
    ])
  }, [datasetId, demoMode, features, loadFeaturePage, notify, selected, selectedLayer])

  const mapFeatures = useMemo(
    () =>
      layers.flatMap((layer) => {
        if (!visibleLayerIds.has(layer.id)) return []
        const color = layerColor(layer.id)
        return (features[layer.id]?.wgs84?.features ?? []).map((feature) => ({
          ...feature,
          properties: {
            ...feature.properties,
            __overlay_layer_id: layer.id,
            __overlay_feature_id: String(feature.id),
            __overlay_color: color,
            __overlay_selected:
              selected?.layerId === layer.id && String(selected.featureId) === String(feature.id)
                ? 1
                : 0,
          },
        }))
      }),
    [features, layerColor, layers, selected, visibleLayerIds],
  )

  const datasetFeatures = useMemo(
    () =>
      layers.flatMap((layer) =>
        visibleLayerIds.has(layer.id)
          ? (features[layer.id]?.dataset?.features ?? []).map((feature) => ({
              layerId: layer.id,
              color: layerColor(layer.id),
              feature,
            }))
          : [],
      ),
    [features, layerColor, layers, visibleLayerIds],
  )

  const value = useMemo<OverlayContextValue>(
    () => ({
      datasetId,
      layers,
      features,
      visibleLayerIds,
      selected,
      selectedLayer,
      selectedFeature,
      selectedDatasetFeature,
      mapFeatures,
      datasetFeatures,
      loading,
      uploading,
      pickMode,
      setPickMode,
      refresh,
      ensureDatasetFeatures,
      loadMoreDatasetFeatures,
      upload,
      removeLayer,
      toggleLayer,
      selectFeature,
      updateSelected,
      applyPickedCoordinate,
      deleteSelected,
      layerColor,
    }),
    [
      applyPickedCoordinate,
      datasetFeatures,
      datasetId,
      deleteSelected,
      features,
      ensureDatasetFeatures,
      layerColor,
      layers,
      loading,
      loadMoreDatasetFeatures,
      mapFeatures,
      pickMode,
      refresh,
      removeLayer,
      selected,
      selectedDatasetFeature,
      selectedFeature,
      selectedLayer,
      selectFeature,
      toggleLayer,
      updateSelected,
      upload,
      uploading,
      visibleLayerIds,
    ],
  )

  return <OverlayContext.Provider value={value}>{children}</OverlayContext.Provider>
}

export function useOverlayWorkspace(): OverlayContextValue {
  const value = useContext(OverlayContext)
  if (!value) throw new Error('useOverlayWorkspace must be used inside OverlayProvider.')
  return value
}

export function useOptionalOverlayWorkspace(): OverlayContextValue | null {
  return useContext(OverlayContext)
}
