import { Eye, EyeOff, Layers3, MapPin, Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useOptionalOverlayWorkspace } from './OverlayContext'

export function DatasetOverlayBrowser({ focusLayerId }: { focusLayerId?: string }) {
  const overlay = useOptionalOverlayWorkspace()
  const [layerQuery, setLayerQuery] = useState('')
  const appliedFocusLayerRef = useRef<string | undefined>(undefined)

  useEffect(() => {
    if (!focusLayerId) {
      appliedFocusLayerRef.current = undefined
      return
    }
    if (
      overlay &&
      appliedFocusLayerRef.current !== focusLayerId &&
      overlay.layers.some((layer) => layer.id === focusLayerId)
    ) {
      appliedFocusLayerRef.current = focusLayerId
      overlay.setActiveLayerId(focusLayerId)
    }
  }, [focusLayerId, overlay?.layers, overlay?.setActiveLayerId])

  const normalizedQuery = layerQuery.trim().toLocaleLowerCase('ko-KR')
  const filteredLayers = useMemo(
    () =>
      (overlay?.layers ?? []).filter((layer) =>
        `${layer.name} ${layer.geometry_type}`
          .toLocaleLowerCase('ko-KR')
          .includes(normalizedQuery),
      ),
    [normalizedQuery, overlay?.layers],
  )

  if (!overlay) return null

  return (
    <section className="dataset-overlay-browser" aria-label="SHP 레이어 표시 설정">
      <div className="section-label">
        <span>SHP 레이어</span>
        <small>{overlay.layers.length.toLocaleString('ko-KR')}개</small>
      </div>

      {overlay.layers.length > 0 ? (
        <>
          <label className="search-box dataset-layer-search">
            <Search size={13} />
            <input
              value={layerQuery}
              onChange={(event) => setLayerQuery(event.target.value)}
              placeholder="도로대장 레이어 검색"
              aria-label="SHP 레이어 검색"
            />
          </label>
          <div className="dataset-layer-list" role="list" aria-label="SHP 레이어 목록">
            {filteredLayers.map((layer) => {
              const visible = overlay.visibleLayerIds.has(layer.id)
              const active = overlay.activeLayerId === layer.id
              return (
                <div
                  key={layer.id}
                  role="listitem"
                  className={`dataset-layer-item ${active ? 'active' : ''}`}
                >
                  <button
                    type="button"
                    className="dataset-layer-item-main"
                    onClick={() => overlay.setActiveLayerId(layer.id)}
                    aria-label={`${layer.name} 레이어 선택`}
                  >
                    <i style={{ background: overlay.layerColor(layer.id) }} />
                    <span>
                      <strong>{layer.name}</strong>
                      <small>
                        {layer.geometry_type} · {layer.feature_count.toLocaleString('ko-KR')}개
                      </small>
                    </span>
                  </button>
                  <button
                    type="button"
                    className={`dataset-layer-visibility ${visible ? 'active' : ''}`}
                    aria-pressed={visible}
                    aria-label={`${layer.name} ${visible ? '숨기기' : '표시'}`}
                    onClick={() => overlay.toggleLayer(layer.id)}
                  >
                    {visible ? <Eye size={14} /> : <EyeOff size={14} />}
                  </button>
                </div>
              )
            })}
            {!filteredLayers.length && (
              <div className="dataset-overlay-empty">검색 결과가 없습니다.</div>
            )}
          </div>
        </>
      ) : (
        <div className="dataset-overlay-empty">
          <MapPin size={18} />
          <span>등록된 SHP 레이어가 없습니다.</span>
        </div>
      )}
    </section>
  )
}
