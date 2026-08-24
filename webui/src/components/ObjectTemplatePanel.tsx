import { BoxSelect, ChevronDown, ChevronUp, Crosshair, LoaderCircle } from 'lucide-react'
import { useState } from 'react'
import type { OverlayFeature, OverlayField } from '../types'
import { useOptionalManualObjectWorkspace } from './ManualObjectContext'
import { useOptionalOverlayWorkspace } from './OverlayContext'

function fieldInputType(field: OverlayField): 'number' | 'text' {
  const type = (field.type ?? '').trim().toUpperCase()
  return ['N', 'F', 'I', 'B', 'NUMBER', 'NUMERIC', 'DECIMAL', 'FLOAT', 'DOUBLE', 'INTEGER', 'INT'].includes(type)
    ? 'number'
    : 'text'
}

const SUPPORT_CLASS_FIELDS = new Set([
  'CLASS',
  'CLASS_NM',
  'CLASSNAME',
  'CLASS_NAME',
  'OBJ_TYPE',
  'TYPE',
])

export function selectedFeatureIsSupportPole(feature: OverlayFeature | null): boolean {
  if (!feature) return false
  const classValue = Object.entries(feature.properties).find(([name]) =>
    SUPPORT_CLASS_FIELDS.has(name.trim().toUpperCase()),
  )?.[1]
  const normalizedClass = String(classValue ?? '').trim().toUpperCase().replace(/[\s-]+/g, '_')
  const creationTool = feature.provenance?.creation_tool ??
    feature.properties.creation_tool ??
    feature.properties.CREATION_TOOL ??
    feature.properties._creation_tool
  return (
    ['SIGN_SUPPORT_POLE', 'SUPPORT_POLE', 'POLE', '지주'].includes(normalizedClass) ||
    creationTool === 'manual_pole_base_v1'
  )
}

export function ObjectTemplatePanel() {
  const manual = useOptionalManualObjectWorkspace()
  const overlay = useOptionalOverlayWorkspace()
  const [collapsed, setCollapsed] = useState(false)
  if (!manual?.enabled) return null

  const targetFields = manual.targetLayer?.fields ??
    (manual.targetLayer ? overlay?.features[manual.targetLayer.id]?.dataset?.fields : undefined) ??
    []
  const fields = targetFields
    .filter((field) => !field.internal)
    .slice(0, 12)
  const supportField = targetFields.find((field) =>
    ['SUPPORT_ID', 'SUPPORTID', 'SUP_ID', 'POLE_ID'].includes(field.name.trim().toUpperCase()),
  )
  const selectedSupport = overlay?.selectedDatasetFeature ?? overlay?.selectedFeature
  const selectedSupportValue = selectedFeatureIsSupportPole(selectedSupport ?? null) && selectedSupport
    ? selectedSupport.properties.FTR_IDN ??
      selectedSupport.properties.OBJECT_ID ??
      selectedSupport.properties.POLE_ID ??
      selectedSupport.properties.ID ??
      selectedSupport.id
    : null

  return (
    <aside className={`manual-template-panel ${collapsed ? 'collapsed' : ''}`} aria-label="객체 추가·수정">
      <header>
        <span><BoxSelect size={15} /><strong>객체 추가·수정</strong></span>
        <button
          type="button"
          aria-label={collapsed ? '객체 추가·수정 펼치기' : '객체 추가·수정 접기'}
          onClick={() => setCollapsed((value) => !value)}
        >
          {collapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
        </button>
      </header>
      {!collapsed && (
        <div className="manual-template-body">
          <label>
            <span>객체 종류</span>
            <select
              aria-label="추가·수정할 객체 종류"
              value={manual.templateId}
              disabled={manual.templatesLoading}
              onChange={(event) => manual.setTemplateId(event.target.value as typeof manual.templateId)}
            >
              {manual.templates.map((template) => (
                <option key={template.template_id} value={template.template_id}>
                  {template.template_id === 'TRAFFIC_SIGN' ? '교통표지판' : '표지 지주'}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>대상 Point 레이어</span>
            <select
              aria-label="수동 객체 대상 레이어"
              value={manual.targetLayerId}
              onChange={(event) => manual.setTargetLayerId(event.target.value)}
            >
              {manual.pointLayers.length === 0 && <option value="">Point 레이어 없음</option>}
              {manual.pointLayers.map((layer) => (
                <option key={layer.id} value={layer.id}>{layer.name}</option>
              ))}
            </select>
          </label>

          {fields.length > 0 && (
            <details className="manual-property-fields">
              <summary>속성 입력 <small>{manual.missingRequiredFields.length ? `필수 ${manual.missingRequiredFields.length}` : '자동 저장'}</small></summary>
              <div>
                {fields.map((field) => {
                  const domain = manual.template.domains?.[field.name] ?? manual.template.property_domains?.[field.name]
                  const value = manual.effectiveProperties[field.name]
                  const locked = Object.prototype.hasOwnProperty.call(
                    manual.template.fixed_values ?? {},
                    field.name,
                  ) || SUPPORT_CLASS_FIELDS.has(field.name.trim().toUpperCase())
                  return (
                    <label key={field.name}>
                      <span>{field.name}{field.required ? <b>*</b> : null}</span>
                      {domain?.length ? (
                        <select
                          value={String(value ?? '')}
                          disabled={locked}
                          onChange={(event) => manual.setProperty(field.name, event.target.value)}
                        >
                          <option value="">선택</option>
                          {domain.map((item) => <option key={String(item)} value={String(item)}>{String(item)}</option>)}
                        </select>
                      ) : (
                        <input
                          type={fieldInputType(field)}
                          value={String(value ?? '')}
                          disabled={locked}
                          aria-label={`${field.name} 속성`}
                          onChange={(event) => manual.setProperty(
                            field.name,
                            fieldInputType(field) === 'number' && event.target.value !== ''
                              ? Number(event.target.value)
                              : event.target.value,
                          )}
                        />
                      )}
                    </label>
                  )
                })}
              </div>
            </details>
          )}

          {manual.templateId === 'TRAFFIC_SIGN' && supportField && selectedSupportValue !== null && (
            <button
              type="button"
              className="button secondary compact manual-support-link"
              onClick={() => manual.setProperty(supportField.name, selectedSupportValue)}
            >
              선택 지주 연결 · {String(selectedSupportValue)}
            </button>
          )}
          {manual.templateId === 'TRAFFIC_SIGN' && supportField && selectedSupportValue === null && (
            <small className="manual-support-hint">SUPPORT_ID를 제안하려면 먼저 지주 Point를 선택하세요.</small>
          )}

          <label className="manual-continuous-toggle">
            <input
              type="checkbox"
              checked={manual.continuous}
              onChange={(event) => manual.setContinuous(event.target.checked)}
            />
            <span>저장 후 연속 추가</span>
          </label>

          <button
            type="button"
            className={`button compact ${manual.bboxMode ? 'primary' : 'secondary'}`}
            disabled={!manual.frame || !manual.targetLayer}
            onClick={manual.startSelectedTemplate}
          >
            {manual.templatesLoading ? <LoaderCircle className="spin" size={14} /> : <Crosshair size={14} />}
            {manual.templateId === 'TRAFFIC_SIGN' ? '파노라마 bbox 시작 (M)' : '점군 지주 생성 시작'}
          </button>
          <small className="manual-template-help">
            {manual.templateId === 'TRAFFIC_SIGN'
              ? '파노라마에서 드래그를 놓으면 3D 위치 계산이 자동으로 시작됩니다.'
              : '기존 P0 방식으로 점을 선택하고 B → B로 저장합니다.'}
          </small>
        </div>
      )}
    </aside>
  )
}
