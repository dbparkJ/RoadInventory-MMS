import {
  Crosshair,
  Download,
  Eye,
  EyeOff,
  FileArchive,
  Layers3,
  LoaderCircle,
  MapPin,
  RefreshCcw,
  Save,
  Table2,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { api } from '../lib/api'
import type { OverlayEncoding, OverlayFeature, OverlayField } from '../types'
import { useOverlayWorkspace } from './OverlayContext'
import './OverlayPanel.css'

function pointCoordinates(feature: OverlayFeature | null): [number, number, number?] | null {
  const coordinates = feature?.geometry?.coordinates
  if (
    feature?.geometry?.type !== 'Point' ||
    !Array.isArray(coordinates) ||
    coordinates.length < 2 ||
    !coordinates.every((value) => typeof value === 'number')
  ) {
    return null
  }
  return [coordinates[0] as number, coordinates[1] as number, coordinates[2] as number | undefined]
}

function fieldName(field: OverlayField | string): string {
  return typeof field === 'string' ? field : field.name
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function coerceOverlayFieldValue(
  value: string,
  field: OverlayField | undefined,
  previous: unknown,
): unknown {
  const type = field?.type?.toUpperCase()
  const trimmed = value.trim()
  if (type === 'N' || type === 'F') {
    if (!trimmed) return null
    const parsed = Number(trimmed)
    if (!Number.isFinite(parsed)) throw new Error(`${field?.name ?? '숫자 필드'}에 유효한 숫자를 입력해 주세요.`)
    return type === 'N' && field?.decimal === 0 ? Math.trunc(parsed) : parsed
  }
  if (type === 'L') {
    if (!trimmed) return null
    if (['true', '1', 'y', 'yes', 't'].includes(trimmed.toLowerCase())) return true
    if (['false', '0', 'n', 'no', 'f'].includes(trimmed.toLowerCase())) return false
    throw new Error(`${field?.name ?? '논리 필드'}는 true 또는 false여야 합니다.`)
  }
  if (type === 'D') {
    if (!trimmed) return null
    if (!/^\d{4}-?\d{2}-?\d{2}$/.test(trimmed)) {
      throw new Error(`${field?.name ?? '날짜 필드'}는 YYYY-MM-DD 형식이어야 합니다.`)
    }
    return trimmed
  }
  if (!field && previous === null && !value) return null
  return value
}

export function OverlayPanel({
  focusLayerId,
  externalAction,
  onClose,
}: {
  focusLayerId?: string
  externalAction?: ReactNode
  onClose: () => void
}) {
  const overlay = useOverlayWorkspace()
  const [files, setFiles] = useState<File[]>([])
  const [name, setName] = useState('')
  const [crs, setCrs] = useState('')
  const [encoding, setEncoding] = useState<OverlayEncoding>('auto')
  const [activeLayerId, setActiveLayerId] = useState('')
  const [query, setQuery] = useState('')
  const [coordinateDraft, setCoordinateDraft] = useState(['', '', ''])
  const [propertyDraft, setPropertyDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  useEffect(() => {
    if (overlay.selected?.layerId) {
      setActiveLayerId(overlay.selected.layerId)
      return
    }
    if (focusLayerId && overlay.layers.some((layer) => layer.id === focusLayerId)) {
      setActiveLayerId(focusLayerId)
      return
    }
    if (!overlay.layers.some((layer) => layer.id === activeLayerId)) {
      setActiveLayerId(overlay.layers[0]?.id ?? '')
    }
  }, [activeLayerId, focusLayerId, overlay.layers, overlay.selected?.layerId])

  const activeLayer = overlay.layers.find((layer) => layer.id === activeLayerId) ?? null
  const datasetCollection = activeLayer
    ? overlay.features[activeLayer.id]?.dataset ?? null
    : null
  const collection = datasetCollection?.features ?? []
  const wgs84Collection = activeLayer
    ? overlay.features[activeLayer.id]?.wgs84 ?? null
    : null
  const moreFeaturesAvailable = Boolean(
    (datasetCollection && datasetCollection.total > collection.length) ||
      (wgs84Collection && wgs84Collection.total > wgs84Collection.features.length),
  )
  const fieldDefinitions = activeLayer
    ? overlay.features[activeLayer.id]?.dataset?.fields ?? []
    : []
  const fields = fieldDefinitions.map(fieldName)
  const fieldsByName = new Map(
    fieldDefinitions.map((field) => [fieldName(field), typeof field === 'string' ? undefined : field]),
  )
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase('ko-KR')
    if (!normalized) return collection
    return collection.filter((feature) =>
      [feature.id, ...Object.values(feature.properties)]
        .map(displayValue)
        .some((value) => value.toLocaleLowerCase('ko-KR').includes(normalized)),
    )
  }, [collection, query])
  const shown = filtered.slice(0, 500)
  const selected = overlay.selectedDatasetFeature
  const selectedIsPoint = selected?.geometry?.type === 'Point'

  useEffect(() => {
    if (activeLayerId) void overlay.ensureDatasetFeatures(activeLayerId)
  }, [activeLayerId, overlay.ensureDatasetFeatures])

  useEffect(() => {
    if (selected && !selectedIsPoint && overlay.pickMode) overlay.setPickMode(false)
  }, [overlay.pickMode, overlay.setPickMode, selected, selectedIsPoint])

  useEffect(() => {
    const coordinates = pointCoordinates(selected)
    setCoordinateDraft(
      coordinates
        ? [String(coordinates[0]), String(coordinates[1]), coordinates[2] === undefined ? '' : String(coordinates[2])]
        : ['', '', ''],
    )
    setPropertyDraft(
      selected
        ? Object.fromEntries(
            Object.entries(selected.properties).map(([key, value]) => [key, displayValue(value)]),
          )
        : {},
    )
    setValidationError(null)
  }, [selected])

  const submitUpload = async (event: FormEvent) => {
    event.preventDefault()
    if (!files.length) return
    setActionError(null)
    try {
      await overlay.upload(files, name, crs, encoding)
      setFiles([])
      setName('')
      setCrs('')
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : 'SHP 레이어를 등록하지 못했습니다.')
    }
  }

  const performAction = async (action: () => Promise<void>) => {
    setActionError(null)
    try {
      await action()
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '요청을 처리하지 못했습니다.')
    }
  }

  const saveSelected = async () => {
    if (!selected) return
    const xText = coordinateDraft[0].trim()
    const yText = coordinateDraft[1].trim()
    const x = Number(coordinateDraft[0])
    const y = Number(coordinateDraft[1])
    const z = coordinateDraft[2].trim() ? Number(coordinateDraft[2]) : undefined
    if (
      selectedIsPoint &&
      (!xText || !yText || !Number.isFinite(x) || !Number.isFinite(y) ||
        (coordinateDraft[2].trim() && !Number.isFinite(z)))
    ) {
      setValidationError('Point 피처의 X/Y는 유효한 숫자가 필요하며 Z는 비워둘 수 있습니다.')
      return
    }
    const previous = selected.properties
    let properties: Record<string, unknown>
    try {
      properties = Object.fromEntries(
        Object.entries(propertyDraft).map(([key, value]) => [
          key,
          coerceOverlayFieldValue(value, fieldsByName.get(key), previous[key]),
        ]),
      )
    } catch (reason) {
      setValidationError(reason instanceof Error ? reason.message : '속성 값을 확인해 주세요.')
      return
    }
    setSaving(true)
    setValidationError(null)
    try {
      await overlay.updateSelected({
        ...(selectedIsPoint
          ? {
              geometry: {
                type: 'Point' as const,
                coordinates: (Number.isFinite(z) ? [x, y, z] : [x, y]) as [
                  number,
                  number,
                  number?,
                ],
              },
              coordinate_space: 'dataset' as const,
            }
          : {}),
        properties,
      })
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : '피처 변경을 저장하지 못했습니다.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="overlay-panel" aria-label="SHP 레이어 및 속성표">
      <header className="overlay-panel-header">
        <div>
          <span className="eyebrow">VECTOR WORKSPACE</span>
          <h2>SHP 레이어 · 속성표</h2>
        </div>
        <div className="overlay-panel-actions">
          {externalAction}
          <button type="button" className="icon-button" onClick={onClose} aria-label="SHP 패널 닫기">
            <X size={17} />
          </button>
        </div>
      </header>

      <form className="overlay-upload" onSubmit={(event) => void submitUpload(event)}>
        <label className="overlay-file-drop">
          <Upload size={18} />
          <span>
            <strong>{files.length ? `${files.length}개 파일 선택됨` : 'SHP 묶음 또는 ZIP 선택'}</strong>
            <small>.shp/.shx/.dbf와 CRS 파일을 함께 선택할 수 있습니다.</small>
          </span>
          <input
            type="file"
            multiple
            accept=".zip,.shp,.shx,.dbf,.prj,.cpg,.qpj,.wkt2"
            onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
          />
        </label>
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="레이어 이름 (선택)" />
        <input value={crs} onChange={(event) => setCrs(event.target.value)} placeholder="CRS 예: EPSG:32652 (PRJ가 없을 때)" />
        <label className="overlay-encoding-select">
          <span>DBF 문자셋</span>
          <select
            aria-label="DBF 문자 인코딩"
            value={encoding}
            onChange={(event) => setEncoding(event.target.value as OverlayEncoding)}
          >
            <option value="auto">자동 감지 (.cpg 우선)</option>
            <option value="CP949">CP949 · 한국어</option>
            <option value="EUC-KR">EUC-KR · 한국어</option>
            <option value="UTF-8">UTF-8</option>
          </select>
        </label>
        <button type="submit" className="button primary" disabled={!files.length || overlay.uploading}>
          {overlay.uploading ? <LoaderCircle className="spin" size={15} /> : <FileArchive size={15} />}
          레이어 등록
        </button>
        <small className="source-preserved">업로드 원본은 보존되고 수정 내용은 별도 편집본에 저장됩니다.</small>
        {actionError && <small className="overlay-action-error">{actionError}</small>}
      </form>

      <div className="overlay-workspace-grid">
        <aside className="overlay-layer-list">
          <header>
            <span><Layers3 size={14} /> 레이어</span>
            <small>{overlay.layers.length}</small>
          </header>
          {overlay.layers.map((layer) => {
            const visible = overlay.visibleLayerIds.has(layer.id)
            const layerState = overlay.features[layer.id]
            return (
              <article key={layer.id} className={activeLayerId === layer.id ? 'active' : ''}>
                <button type="button" className="overlay-layer-main" onClick={() => setActiveLayerId(layer.id)}>
                  <i style={{ background: overlay.layerColor(layer.id) }} />
                  <span>
                    <strong>{layer.name}</strong>
                    <small>
                      {layer.feature_count.toLocaleString('ko-KR')} · {layer.geometry_type}
                      {layer.source_encoding ? ` · ${layer.source_encoding}` : ''}
                    </small>
                    {Boolean(layer.warnings?.length) && (
                      <small className="overlay-layer-warning" title={layer.warnings?.join('\n')}>
                        경고 {layer.warnings?.length}개 · 확인 필요
                      </small>
                    )}
                    {layerState?.error && (
                      <small className="overlay-layer-error" title={layerState.error}>
                        피처 로드 오류 · 재시도 필요
                      </small>
                    )}
                  </span>
                </button>
                <div>
                  <button type="button" onClick={() => overlay.toggleLayer(layer.id)} title={visible ? '숨기기' : '표시'}>
                    {visible ? <Eye size={14} /> : <EyeOff size={14} />}
                  </button>
                  {layerState?.error && (
                    <button
                      type="button"
                      onClick={() => void performAction(overlay.refresh)}
                      title="SHP 피처 다시 불러오기"
                    >
                      <RefreshCcw size={14} />
                    </button>
                  )}
                  <a href={api.overlayDownloadUrl(overlay.datasetId, layer.id)} title="현재 편집본 SHP ZIP 다운로드">
                    <Download size={14} />
                  </a>
                  <button
                    type="button"
                    className="danger-action"
                    title="레이어 등록 제거"
                    onClick={() => {
                      if (window.confirm(`${layer.name} 레이어를 목록에서 제거할까요?\n업로드 원본은 보존됩니다.`)) {
                        void performAction(() => overlay.removeLayer(layer.id))
                      }
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </article>
            )
          })}
          {!overlay.layers.length && !overlay.loading && (
            <div className="overlay-empty"><MapPin size={22} /><span>등록된 SHP 레이어가 없습니다.</span></div>
          )}
        </aside>

        <div className="overlay-table-area">
          <div className="overlay-table-toolbar">
            <span><Table2 size={14} /> 속성표</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="피처 검색" />
            <small>
              {filtered.length.toLocaleString('ko-KR')}
              {datasetCollection && datasetCollection.total > collection.length
                ? ` / ${datasetCollection.total.toLocaleString('ko-KR')}`
                : ''}
              행
            </small>
          </div>
          <div className="overlay-table-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>X</th>
                  <th>Y</th>
                  <th>Z</th>
                  {fields.slice(0, 6).map((field) => <th key={field}>{field}</th>)}
                </tr>
              </thead>
              <tbody>
                {shown.map((feature) => {
                  const coordinates = pointCoordinates(feature)
                  const isSelected =
                    overlay.selected?.layerId === activeLayerId &&
                    String(overlay.selected.featureId) === String(feature.id)
                  return (
                    <tr
                      key={String(feature.id)}
                      className={isSelected ? 'selected' : ''}
                      onClick={() => overlay.selectFeature({ layerId: activeLayerId, featureId: feature.id })}
                    >
                      <td>{String(feature.id)}</td>
                      <td>{coordinates?.[0]?.toFixed(3) ?? '—'}</td>
                      <td>{coordinates?.[1]?.toFixed(3) ?? '—'}</td>
                      <td>{coordinates?.[2]?.toFixed(3) ?? '—'}</td>
                      {fields.slice(0, 6).map((field) => <td key={field}>{displayValue(feature.properties[field])}</td>)}
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {filtered.length > shown.length && <p className="overlay-table-limit">성능을 위해 검색 결과 중 첫 500행을 표시합니다.</p>}
            {moreFeaturesAvailable && datasetCollection && (
              <button
                type="button"
                className="button secondary overlay-load-more"
                disabled={Boolean(activeLayer && overlay.features[activeLayer.id]?.loadingDataset)}
                onClick={() => void performAction(() => overlay.loadMoreDatasetFeatures(activeLayerId))}
              >
                <LoaderCircle
                  size={14}
                  className={activeLayer && overlay.features[activeLayer.id]?.loadingDataset ? 'spin' : ''}
                />
                다음 피처 불러오기 · 현재 {collection.length.toLocaleString('ko-KR')} /{' '}
                {datasetCollection.total.toLocaleString('ko-KR')}
                {wgs84Collection && wgs84Collection.features.length < wgs84Collection.total
                  ? ` · 지도 ${wgs84Collection.features.length.toLocaleString('ko-KR')}`
                  : ''}
              </button>
            )}
          </div>
        </div>

        <aside className="overlay-feature-editor">
          <header>
            <span><Crosshair size={14} /> 선택 피처 편집</span>
            {selected && <code>{String(selected.id)}</code>}
          </header>
          {!selected ? (
            <div className="overlay-empty"><span>속성표 또는 지도에서 피처를 선택해 주세요.</span></div>
          ) : (
            <>
              <div className="coordinate-fields">
                {(['X', 'Y', 'Z'] as const).map((axis, index) => (
                  <label key={axis}>
                    <span>{axis}</span>
                    <input
                      inputMode="decimal"
                      value={coordinateDraft[index]}
                      disabled={!selectedIsPoint}
                      onChange={(event) =>
                        setCoordinateDraft((current) => current.map((value, item) => item === index ? event.target.value : value))
                      }
                    />
                  </label>
                ))}
              </div>
              <button
                type="button"
                className={`button ${overlay.pickMode ? 'primary' : 'secondary'} overlay-pick-button`}
                disabled={!selectedIsPoint}
                onClick={() => overlay.setPickMode(!overlay.pickMode)}
              >
                <Crosshair size={15} />
                {overlay.pickMode ? '위치 지정 취소' : '뷰에서 실제 포인트 선택 (P)'}
              </button>
              {overlay.pickMode && (
                <p className="pick-instruction">지도 위치 또는 파노라마/3D의 점군을 클릭하세요. Esc로 취소할 수 있습니다.</p>
              )}
              {!selectedIsPoint && (
                <p className="pick-instruction">선·면 피처는 속성만 수정할 수 있습니다. 좌표 지정은 Point 피처에서 사용하세요.</p>
              )}
              {validationError && <p className="overlay-validation-error">{validationError}</p>}
              <div className="property-editor">
                {Object.keys(propertyDraft).map((key) => (
                  <label key={key}>
                    <span>{key}</span>
                    {fieldsByName.get(key)?.type?.toUpperCase() === 'L' ? (
                      <select
                        value={propertyDraft[key]}
                        onChange={(event) => setPropertyDraft((current) => ({ ...current, [key]: event.target.value }))}
                      >
                        <option value="">NULL</option>
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    ) : (
                      <input
                        type={
                          ['N', 'F'].includes(fieldsByName.get(key)?.type?.toUpperCase() ?? '')
                            ? 'number'
                            : fieldsByName.get(key)?.type?.toUpperCase() === 'D'
                              ? 'date'
                              : 'text'
                        }
                        step={fieldsByName.get(key)?.decimal ? 'any' : undefined}
                        value={propertyDraft[key]}
                        onChange={(event) => setPropertyDraft((current) => ({ ...current, [key]: event.target.value }))}
                      />
                    )}
                  </label>
                ))}
              </div>
              <footer>
                <button type="button" className="button primary" disabled={saving} onClick={() => void saveSelected()}>
                  {saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}
                  변경 저장
                </button>
                <button
                  type="button"
                  className="button danger"
                  onClick={() => {
                    if (window.confirm('선택한 피처를 편집본에서 삭제할까요?')) {
                      void performAction(overlay.deleteSelected)
                    }
                  }}
                >
                  <Trash2 size={14} /> 삭제
                </button>
              </footer>
            </>
          )}
        </aside>
      </div>
    </section>
  )
}
