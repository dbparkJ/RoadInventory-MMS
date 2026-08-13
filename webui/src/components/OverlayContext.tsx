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
  OverlayFeatureCreateRequest,
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

interface SelectedFeatureDetailCache {
  datasetId: string
  layerId: string
  featureId: string | number
  revision: number
  feature: OverlayFeature
}

export interface OverlaySelection {
  layerId: string
  featureId: string | number
}

export interface OverlaySelectionOptions {
  /**
   * Whether selecting the feature should also locate and open its MMS frame.
   * Attribute-only actions such as the hover tooltip's Details button disable
   * this so opening the editor cannot reset every viewer.
   */
  navigate?: boolean
}

export type OverlayPickTarget =
  | { kind: 'move'; layerId: string; featureId: string | number }
  | { kind: 'create'; layerId: string }

export interface OverlayContextValue {
  datasetId: string
  layers: OverlayLayer[]
  features: Record<string, LayerFeatures>
  visibleLayerIds: Set<string>
  activeLayerId: string
  setActiveLayerId: (layerId: string) => void
  selected: OverlaySelection | null
  selectedLayer: OverlayLayer | null
  selectedFeature: OverlayFeature | null
  selectedDatasetFeature: OverlayFeature | null
  mapFeatures: OverlayFeature[]
  datasetFeatures: Array<{ layerId: string; color: string; feature: OverlayFeature }>
  loading: boolean
  uploading: boolean
  creatingFeature: boolean
  pickMode: boolean
  pickTarget: OverlayPickTarget | null
  setPickMode: (enabled: boolean) => void
  beginCreatePoint: (layerId: string) => void
  refresh: () => Promise<void>
  ensureDatasetFeatures: (layerId: string) => Promise<void>
  loadMoreDatasetFeatures: (layerId: string) => Promise<void>
  upload: (files: File[], name?: string, crs?: string, encoding?: OverlayEncoding) => Promise<void>
  updateLayerMetadata: (layerId: string, patch: { name?: string; color?: string }) => Promise<void>
  removeLayer: (layerId: string) => Promise<void>
  toggleLayer: (layerId: string) => void
  selectFeature: (
    selection: OverlaySelection | null,
    options?: OverlaySelectionOptions,
  ) => void
  updateSelected: (patch: {
    geometry?: { type: 'Point'; coordinates: [number, number, number?] }
    coordinate_space?: OverlayCoordinateSpace
    properties?: Record<string, unknown>
  }) => Promise<void>
  applyPickedCoordinate: (
    coordinates: [number, number, number?],
    coordinateSpace: OverlayCoordinateSpace,
  ) => Promise<void>
  copySelectedLocation: () => Promise<void>
  deleteSelected: () => Promise<void>
  deleteField: (layerId: string, fieldName: string) => Promise<void>
  layerColor: (layerId: string) => string
}

const OverlayContext = createContext<OverlayContextValue | null>(null)

function featureId(feature: OverlayFeature): string {
  return String(feature.id)
}

function detailFeatureForSelection(
  detail: SelectedFeatureDetailCache | null,
  datasetId: string,
  selection: OverlaySelection | null,
  layerRevision: number | undefined,
): OverlayFeature | null {
  if (
    !detail ||
    !selection ||
    layerRevision === undefined ||
    detail.datasetId !== datasetId ||
    detail.layerId !== selection.layerId ||
    String(detail.featureId) !== String(selection.featureId) ||
    detail.revision < layerRevision
  ) {
    return null
  }
  return detail.feature
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
  const [activeLayerId, setActiveLayerId] = useState('')
  const [selected, setSelected] = useState<OverlaySelection | null>(null)
  const [selectedDatasetDetail, setSelectedDatasetDetail] =
    useState<SelectedFeatureDetailCache | null>(null)
  const [selectedWgs84Detail, setSelectedWgs84Detail] =
    useState<SelectedFeatureDetailCache | null>(null)
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [creatingFeature, setCreatingFeature] = useState(false)
  const [pickTarget, setPickTarget] = useState<OverlayPickTarget | null>(null)
  const pickMode = pickTarget !== null
  const requestGeneration = useRef(0)
  const refreshControllerRef = useRef<AbortController | null>(null)
  const loadedDatasetRef = useRef('')
  const knownLayerIdsRef = useRef<Set<string>>(new Set())
  const coordinateLoadsRef = useRef<Set<string>>(new Set())
  const selectionRequestRef = useRef(0)
  const selectionControllerRef = useRef<AbortController | null>(null)
  const visibleLayerIdsRef = useRef(visibleLayerIds)
  visibleLayerIdsRef.current = visibleLayerIds

  const clearSelectedDetails = useCallback(() => {
    selectionRequestRef.current += 1
    selectionControllerRef.current?.abort()
    selectionControllerRef.current = null
    setSelectedDatasetDetail(null)
    setSelectedWgs84Detail(null)
  }, [])

  const layerColor = useCallback(
    (layerId: string) => {
      const savedColor = layers.find((layer) => layer.id === layerId)?.color
      if (savedColor) return savedColor
      let hash = 0
      for (let index = 0; index < layerId.length; index += 1) {
        hash = (hash * 31 + layerId.charCodeAt(index)) | 0
      }
      return LAYER_COLORS[Math.abs(hash) % LAYER_COLORS.length]
    },
    [layers],
  )

  const setPickMode = useCallback(
    (enabled: boolean) => {
      if (!enabled) {
        setPickTarget(null)
        return
      }
      if (!selected) {
        notify?.({ tone: 'info', title: '먼저 위치를 수정할 SHP 피처를 선택해 주세요.' })
        return
      }
      setPickTarget({
        kind: 'move',
        layerId: selected.layerId,
        featureId: selected.featureId,
      })
    },
    [notify, selected],
  )

  const beginCreatePoint = useCallback(
    (layerId: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!layer) {
        notify?.({ tone: 'error', title: '신규 피처를 추가할 SHP 레이어가 없습니다.' })
        return
      }
      if (demoMode) {
        notify?.({ tone: 'info', title: '데모 모드에서는 SHP 피처를 추가할 수 없습니다.' })
        return
      }
      if (layer.geometry_type !== 'Point') {
        notify?.({ tone: 'info', title: '지도 클릭 신규 추가는 Point 레이어에서만 사용할 수 있습니다.' })
        return
      }
      setActiveLayerId(layerId)
      setPickTarget({ kind: 'create', layerId })
    },
    [demoMode, layers, notify],
  )

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
      clearSelectedDetails()
      setLayers([])
      setFeatures({})
      setVisibleLayerIds(new Set())
      setActiveLayerId('')
      setSelected(null)
      setPickTarget(null)
      return
    }
    const controller = new AbortController()
    refreshControllerRef.current = controller
    if (loadedDatasetRef.current !== datasetId) {
      loadedDatasetRef.current = datasetId
      knownLayerIdsRef.current = new Set()
      clearSelectedDetails()
      setLayers([])
      setFeatures({})
      setVisibleLayerIds(new Set())
      setActiveLayerId('')
      setSelected(null)
      setPickTarget(null)
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
      setActiveLayerId((current) =>
        current && nextLayerIds.has(current) ? current : (nextLayers[0]?.id ?? ''),
      )
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
      const keepCurrentDetail = (detail: SelectedFeatureDetailCache | null) => {
        if (!detail || detail.datasetId !== datasetId) return null
        const layer = nextLayers.find((candidate) => candidate.id === detail.layerId)
        return layer && detail.revision >= layer.revision ? detail : null
      }
      setSelectedDatasetDetail(keepCurrentDetail)
      setSelectedWgs84Detail(keepCurrentDetail)
      setPickTarget((current) =>
        current && nextLayerIds.has(current.layerId) ? current : null,
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
  }, [clearSelectedDetails, datasetId, demoMode, loadFeaturePage, notify])

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

  const shortcutStateRef = useRef({
    activeLayerId,
    beginCreatePoint,
    pickMode,
    pickTarget,
    selected,
    setPickMode,
  })
  shortcutStateRef.current = {
    activeLayerId,
    beginCreatePoint,
    pickMode,
    pickTarget,
    selected,
    setPickMode,
  }

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
      const shortcut = shortcutStateRef.current
      if (event.key === 'Escape' && shortcut.pickMode) {
        event.preventDefault()
        shortcut.setPickMode(false)
      } else if (event.code === 'KeyN') {
        event.preventDefault()
        if (event.repeat) return
        if (
          shortcut.pickTarget?.kind === 'create' &&
          shortcut.pickTarget.layerId === shortcut.activeLayerId
        ) {
          shortcut.setPickMode(false)
        } else {
          shortcut.beginCreatePoint(shortcut.activeLayerId)
        }
      } else if (event.code === 'KeyP' && shortcut.selected) {
        event.preventDefault()
        shortcut.setPickMode(!shortcut.pickMode)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

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
        setActiveLayerId(response.layer.id)
        clearSelectedDetails()
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
    [clearSelectedDetails, datasetId, demoMode, notify, refresh],
  )

  const removeLayer = useCallback(
    async (layerId: string) => {
      if (!datasetId || demoMode) return
      await api.deleteOverlay(datasetId, layerId)
      if (selected?.layerId === layerId) {
        clearSelectedDetails()
        setSelected(null)
      }
      if (activeLayerId === layerId) setActiveLayerId('')
      setPickTarget((current) => (current?.layerId === layerId ? null : current))
      notify?.({ tone: 'info', title: 'SHP 레이어 등록을 제거했습니다', message: '업로드 원본은 보존됩니다.' })
      await refresh()
    },
    [activeLayerId, clearSelectedDetails, datasetId, demoMode, notify, refresh, selected?.layerId],
  )

  const updateLayerMetadata = useCallback(
    async (layerId: string, patch: { name?: string; color?: string }) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!datasetId || !layer || demoMode) return
      try {
        const response = await api.patchOverlay(datasetId, layerId, {
          ...patch,
          expected_metadata_revision: layer.metadata_revision ?? 1,
        })
        setLayers((current) =>
          current.map((candidate) =>
            candidate.id === response.layer.id ? response.layer : candidate,
          ),
        )
        notify?.({ tone: 'success', title: 'SHP 레이어 표시 설정을 저장했습니다.' })
      } catch (reason) {
        await refresh()
        notify?.({
          tone: 'error',
          title: 'SHP 레이어 표시 설정을 저장하지 못했습니다.',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      }
    },
    [datasetId, demoMode, layers, notify, refresh],
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
    (selection: OverlaySelection | null, options?: OverlaySelectionOptions) => {
      clearSelectedDetails()
      setSelected(selection)
      if (!selection) return
      setActiveLayerId(selection.layerId)
      if (!visibleLayerIdsRef.current.has(selection.layerId)) {
        setVisibleLayerIds((current) => new Set(current).add(selection.layerId))
        const layer = layers.find((candidate) => candidate.id === selection.layerId)
        if (layer && !features[selection.layerId]?.wgs84 && !features[selection.layerId]?.loadingWgs84) {
          void loadFeaturePage(layer, 'wgs84', requestGeneration.current)
        }
      }
      if (options?.navigate !== false) {
        window.dispatchEvent(
          new CustomEvent('mms-overlay-selected', { detail: { datasetId, selection } }),
        )
      }
    },
    [clearSelectedDetails, datasetId, features, layers, loadFeaturePage],
  )

  const selectedLayer = useMemo(
    () => layers.find((layer) => layer.id === selected?.layerId) ?? null,
    [layers, selected?.layerId],
  )
  const selectedWgs84PageFeature = useMemo(
    () =>
      selected
        ? features[selected.layerId]?.wgs84?.features.find(
            (feature) => featureId(feature) === String(selected.featureId),
          ) ?? null
        : null,
    [features, selected],
  )
  const selectedDatasetPageFeature = useMemo(
    () =>
      selected
        ? features[selected.layerId]?.dataset?.features.find(
            (feature) => featureId(feature) === String(selected.featureId),
          ) ?? null
        : null,
    [features, selected],
  )
  const selectedWgs84DetailFeature = useMemo(
    () =>
      detailFeatureForSelection(
        selectedWgs84Detail,
        datasetId,
        selected,
        selectedLayer?.revision,
      ),
    [datasetId, selected, selectedLayer?.revision, selectedWgs84Detail],
  )
  const selectedDatasetDetailFeature = useMemo(
    () =>
      detailFeatureForSelection(
        selectedDatasetDetail,
        datasetId,
        selected,
        selectedLayer?.revision,
      ),
    [datasetId, selected, selectedDatasetDetail, selectedLayer?.revision],
  )

  useEffect(() => {
    if (!selected || !selectedLayer || !datasetId || demoMode) {
      selectionControllerRef.current?.abort()
      selectionControllerRef.current = null
      return
    }
    const coordinateSpaces: OverlayCoordinateSpace[] = []
    if (!selectedDatasetPageFeature && !selectedDatasetDetailFeature) {
      coordinateSpaces.push('dataset')
    }
    if (!selectedWgs84PageFeature && !selectedWgs84DetailFeature) {
      coordinateSpaces.push('wgs84')
    }
    if (coordinateSpaces.length === 0) return

    const requestId = selectionRequestRef.current + 1
    selectionRequestRef.current = requestId
    selectionControllerRef.current?.abort()
    const controller = new AbortController()
    selectionControllerRef.current = controller
    void Promise.allSettled(
      coordinateSpaces.map(async (coordinateSpace) => ({
        coordinateSpace,
        response: await api.overlayFeature(
          datasetId,
          selected.layerId,
          selected.featureId,
          coordinateSpace,
          controller.signal,
        ),
      })),
    ).then((results) => {
      if (selectionRequestRef.current !== requestId || controller.signal.aborted) return
      const failures: unknown[] = []
      results.forEach((result) => {
        if (result.status === 'rejected') {
          failures.push(result.reason)
          return
        }
        const { coordinateSpace, response } = result.value
        if (response.revision < selectedLayer.revision) return
        const detail: SelectedFeatureDetailCache = {
          datasetId,
          layerId: selected.layerId,
          featureId: selected.featureId,
          revision: response.revision,
          feature: response.feature,
        }
        if (coordinateSpace === 'wgs84') setSelectedWgs84Detail(detail)
        else setSelectedDatasetDetail(detail)
      })
      if (failures.length > 0) {
        const reason = failures[0]
        notify?.({
          tone: 'error',
          title:
            failures.length === coordinateSpaces.length
              ? '선택한 SHP 피처를 불러오지 못했습니다'
              : '선택한 SHP 피처의 일부 좌표를 불러오지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
      }
    })
    return () => {
      controller.abort()
      if (selectionControllerRef.current === controller) selectionControllerRef.current = null
    }
  }, [
    datasetId,
    demoMode,
    notify,
    selected,
    selectedDatasetDetailFeature,
    selectedDatasetPageFeature,
    selectedLayer,
    selectedWgs84DetailFeature,
    selectedWgs84PageFeature,
  ])
  const selectedFeature = useMemo(
    () =>
      selected
        ? selectedWgs84PageFeature ?? selectedWgs84DetailFeature
        : null,
    [selected, selectedWgs84DetailFeature, selectedWgs84PageFeature],
  )
  const selectedDatasetFeature = useMemo(
    () =>
      selected
        ? selectedDatasetPageFeature ?? selectedDatasetDetailFeature
        : null,
    [selected, selectedDatasetDetailFeature, selectedDatasetPageFeature],
  )

  const createFeature = useCallback(
    async (layerId: string, payload: Omit<OverlayFeatureCreateRequest, 'expected_revision'>) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!datasetId || !layer || demoMode) return
      const currentRevision =
        features[layerId]?.dataset?.revision ??
        features[layerId]?.wgs84?.revision ??
        layer.revision
      setCreatingFeature(true)
      try {
        const response = await api.createOverlayFeature(datasetId, layerId, {
          ...payload,
          expected_revision: currentRevision,
        })
        const nextSelection = { layerId, featureId: response.feature.id }
        selectFeature(nextSelection)
        const detail: SelectedFeatureDetailCache = {
          datasetId,
          ...nextSelection,
          revision: response.revision,
          feature: response.feature,
        }
        if (response.coordinate_space === 'dataset') setSelectedDatasetDetail(detail)
        else setSelectedWgs84Detail(detail)
        const generation = requestGeneration.current
        const reloadDataset = Boolean(features[layerId]?.dataset)
        await Promise.all([
          loadFeaturePage(layer, 'wgs84', generation),
          ...(reloadDataset ? [loadFeaturePage(layer, 'dataset', generation)] : []),
        ])
        notify?.({ tone: 'success', title: '신규 SHP 피처를 추가했습니다' })
      } catch (reason) {
        notify?.({
          tone: 'error',
          title: '신규 SHP 피처를 추가하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      } finally {
        setCreatingFeature(false)
      }
    },
    [datasetId, demoMode, features, layers, loadFeaturePage, notify, selectFeature],
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
        const detail: SelectedFeatureDetailCache = {
          datasetId,
          ...selected,
          revision: response.revision,
          feature: response.feature,
        }
        clearSelectedDetails()
        if (response.coordinate_space === 'dataset') setSelectedDatasetDetail(detail)
        else setSelectedWgs84Detail(detail)
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
    [clearSelectedDetails, datasetId, demoMode, features, loadFeaturePage, notify, selected, selectedLayer],
  )

  const applyPickedCoordinate = useCallback(
    async (
      coordinates: [number, number, number?],
      coordinateSpace: OverlayCoordinateSpace,
    ) => {
      const target = pickTarget
      if (!target) return
      if (target.kind === 'create') {
        try {
          await createFeature(target.layerId, {
            geometry: { type: 'Point', coordinates },
            coordinate_space: coordinateSpace,
          })
          setPickTarget(null)
        } catch {
          // createFeature already emitted the actionable server error.
        }
        return
      }
      if (
        !selected ||
        selected.layerId !== target.layerId ||
        String(selected.featureId) !== String(target.featureId)
      ) {
        notify?.({ tone: 'info', title: '먼저 속성표에서 수정할 피처를 선택해 주세요.' })
        setPickTarget(null)
        return
      }
      try {
        await updateSelected({
          geometry: { type: 'Point', coordinates },
          coordinate_space: coordinateSpace,
        })
        setPickTarget(null)
      } catch {
        // updateSelected already emitted the actionable server error.
      }
    },
    [createFeature, notify, pickTarget, selected, updateSelected],
  )

  const copySelectedLocation = useCallback(async () => {
    if (!selected || !selectedLayer) {
      notify?.({ tone: 'info', title: '위치를 복사할 SHP 피처를 먼저 선택해 주세요.' })
      return
    }
    await createFeature(selected.layerId, {
      copy_geometry_from: selected.featureId,
      coordinate_space: 'dataset',
    })
  }, [createFeature, notify, selected, selectedLayer])

  const deleteSelected = useCallback(async () => {
    if (!datasetId || !selected || !selectedLayer || demoMode) return
    const currentRevision = features[selected.layerId]?.dataset?.revision ?? selectedLayer.revision
    await api.deleteOverlayFeature(datasetId, selected.layerId, selected.featureId, currentRevision)
    clearSelectedDetails()
    setSelected(null)
    setPickTarget(null)
    notify?.({ tone: 'info', title: '선택한 SHP 피처를 삭제했습니다' })
    const generation = requestGeneration.current
    const reloadDataset = Boolean(features[selected.layerId]?.dataset)
    await Promise.all([
      loadFeaturePage(selectedLayer, 'wgs84', generation),
      ...(reloadDataset ? [loadFeaturePage(selectedLayer, 'dataset', generation)] : []),
    ])
  }, [clearSelectedDetails, datasetId, demoMode, features, loadFeaturePage, notify, selected, selectedLayer])

  const deleteField = useCallback(
    async (layerId: string, fieldName: string) => {
      const layer = layers.find((candidate) => candidate.id === layerId)
      if (!datasetId || !layer || demoMode) return
      const currentRevision =
        features[layerId]?.dataset?.revision ??
        features[layerId]?.wgs84?.revision ??
        layer.revision
      try {
        await api.deleteOverlayField(datasetId, layerId, fieldName, currentRevision)
        if (selected?.layerId === layerId) clearSelectedDetails()
        notify?.({
          tone: 'info',
          title: `SHP 속성 열 '${fieldName}'을 삭제했습니다`,
          message: '업로드 원본은 보존됩니다.',
        })
        await refresh()
      } catch (reason) {
        await refresh()
        notify?.({
          tone: 'error',
          title: 'SHP 속성 열을 삭제하지 못했습니다',
          message: reason instanceof Error ? reason.message : undefined,
        })
        throw reason
      }
    },
    [clearSelectedDetails, datasetId, demoMode, features, layers, notify, refresh, selected?.layerId],
  )

  const mapFeatures = useMemo(
    () =>
      layers.flatMap((layer) => {
        if (!visibleLayerIds.has(layer.id)) return []
        const color = layerColor(layer.id)
        const pageFeatures = features[layer.id]?.wgs84?.features ?? []
        const selectedForLayer = selected?.layerId === layer.id ? selectedFeature : null
        const layerFeatures =
          selectedForLayer &&
          !pageFeatures.some(
            (feature) => featureId(feature) === String(selectedForLayer.id),
          )
            ? [...pageFeatures, selectedForLayer]
            : pageFeatures
        return layerFeatures.map((feature) => ({
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
    [features, layerColor, layers, selected, selectedFeature, visibleLayerIds],
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
      activeLayerId,
      setActiveLayerId,
      selected,
      selectedLayer,
      selectedFeature,
      selectedDatasetFeature,
      mapFeatures,
      datasetFeatures,
      loading,
      uploading,
      creatingFeature,
      pickMode,
      pickTarget,
      setPickMode,
      beginCreatePoint,
      refresh,
      ensureDatasetFeatures,
      loadMoreDatasetFeatures,
      upload,
      updateLayerMetadata,
      removeLayer,
      toggleLayer,
      selectFeature,
      updateSelected,
      applyPickedCoordinate,
      copySelectedLocation,
      deleteSelected,
      deleteField,
      layerColor,
    }),
    [
      applyPickedCoordinate,
      activeLayerId,
      beginCreatePoint,
      copySelectedLocation,
      creatingFeature,
      datasetFeatures,
      datasetId,
      deleteSelected,
      deleteField,
      features,
      ensureDatasetFeatures,
      layerColor,
      layers,
      loading,
      loadMoreDatasetFeatures,
      mapFeatures,
      pickMode,
      pickTarget,
      refresh,
      removeLayer,
      selected,
      selectedDatasetFeature,
      selectedFeature,
      selectedLayer,
      selectFeature,
      setPickMode,
      toggleLayer,
      updateSelected,
      updateLayerMetadata,
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
