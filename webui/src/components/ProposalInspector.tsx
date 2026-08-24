import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  History,
  LoaderCircle,
  Redo2,
  RotateCcw,
  Undo2,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { OverlayEditHistoryItem } from '../types'
import { useOptionalManualObjectWorkspace } from './ManualObjectContext'
import {
  poleBaseReasonMessages,
  poleBaseTemplateValidationBlocksSave,
  useOptionalOverlayWorkspace,
} from './OverlayContext'
import { useOptionalReviewWorkspace } from './ReviewContext'

const ORIGINAL_HISTORY_ACTIONS = new Set(['create', 'manual_create', 'update', 'delete'])

function operationKey(operation: 'undo' | 'redo'): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}_${Math.random().toString(16).slice(2)}`
  return `${operation}_${random}`
}

export function ProposalInspector() {
  const manual = useOptionalManualObjectWorkspace()
  const overlay = useOptionalOverlayWorkspace()
  const review = useOptionalReviewWorkspace()
  const [collapsed, setCollapsed] = useState(false)
  const [history, setHistory] = useState<OverlayEditHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const layer = manual?.targetLayer ?? null
  const proposalState = manual?.proposalState

  const loadHistory = useCallback(async () => {
    if (!manual?.enabled || !overlay || !layer) {
      setHistory([])
      return
    }
    setHistoryLoading(true)
    setHistoryError('')
    try {
      const response = await api.overlayEditHistory(overlay.datasetId, layer.id, 12)
      setHistory(response.items)
    } catch (reason) {
      setHistoryError(reason instanceof Error ? reason.message : '편집 이력을 불러오지 못했습니다.')
    } finally {
      setHistoryLoading(false)
    }
  }, [layer, manual?.enabled, overlay])

  useEffect(() => {
    void loadHistory()
  }, [layer?.id, layer?.revision]) // eslint-disable-line react-hooks/exhaustive-deps

  const mutateHistory = async (operation: 'undo' | 'redo') => {
    if (!overlay || !layer || historyLoading) return
    setHistoryLoading(true)
    setHistoryError('')
    const expectedRevision =
      overlay.features[layer.id]?.dataset?.revision ??
      overlay.features[layer.id]?.wgs84?.revision ??
      layer.revision
    try {
      const response = await api.mutateOverlayHistory(
        operation,
        overlay.datasetId,
        layer.id,
        {
          expected_revision: expectedRevision,
          idempotency_key: operationKey(operation),
          actor: 'operator-local',
        },
      )
      await overlay.refresh()
      if (response.feature) {
        overlay.selectFeature({ layerId: layer.id, featureId: response.feature_id }, { navigate: false })
      } else if (overlay.selected?.layerId === layer.id && String(overlay.selected.featureId) === response.feature_id) {
        overlay.selectFeature(null)
      }
      review?.reload()
      await loadHistory()
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) await overlay.refresh()
      setHistoryError(
        reason instanceof ApiError && reason.status === 409
          ? '레이어가 변경되어 최신 내용을 불러왔습니다. 다시 시도해 주세요.'
          : reason instanceof Error ? reason.message : '편집 이력을 변경하지 못했습니다.',
      )
    } finally {
      setHistoryLoading(false)
    }
  }

  const canUndo = history.some((item) => ORIGINAL_HISTORY_ACTIONS.has(item.action) && !item.undone)
  const canRedo = history.some((item) => ORIGINAL_HISTORY_ACTIONS.has(item.action) && item.undone)
  const poleProposal = overlay?.poleBaseProposal
  const poleTemplateValidation =
    poleProposal?.status === 'ready' ? poleProposal.templateValidation : undefined
  const visible = Boolean(manual?.enabled && (proposalState?.status !== 'idle' || poleProposal?.status !== 'idle' || layer))
  const hasEssentialFeedback = Boolean(
    (proposalState && proposalState.status !== 'idle') ||
    (poleProposal && poleProposal.status !== 'idle'),
  )
  const inspectorCollapsed = collapsed && !hasEssentialFeedback
  const proposalQuality =
    proposalState?.status === 'ready' || proposalState?.status === 'committing'
      ? proposalState.data.proposal.quality
      : null
  const position =
    proposalState?.status === 'ready' || proposalState?.status === 'committing'
      ? proposalState.data.proposal.geometry?.coordinates
      : null
  const recentHistory = useMemo(() => history.slice(0, 4), [history])
  if (!visible || !manual || !overlay) return null

  return (
    <aside className={`proposal-inspector ${inspectorCollapsed ? 'collapsed' : ''}`} aria-label="제안 검사기">
      <header>
        <span>
          <History size={15} />
          <strong>제안 · 편집 이력</strong>
          {proposalState && proposalState.status !== 'idle' && (
            <small role="status">
              {proposalState.status === 'drawing'
                ? 'bbox 선택'
                : proposalState.status === 'loading'
                  ? '3D 계산 중'
                  : proposalState.status === 'error'
                    ? '계산 실패'
                    : proposalState.status === 'committing'
                      ? '저장 중'
                      : '확인 대기'}
            </small>
          )}
        </span>
        <button
          type="button"
          aria-label={inspectorCollapsed ? '제안 검사기 펼치기' : '제안 검사기 접기'}
          aria-disabled={hasEssentialFeedback}
          title={hasEssentialFeedback ? '진행 중인 제안 상태는 접을 수 없습니다.' : undefined}
          onClick={() => {
            if (!hasEssentialFeedback) setCollapsed((value) => !value)
          }}
        >
          {inspectorCollapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </header>
      {!inspectorCollapsed && (
        <div className="proposal-inspector-body">
          {proposalState?.status === 'drawing' && <p>파노라마에서 객체를 사각형으로 드래그하세요.</p>}
          {proposalState?.status === 'adjusting' && (
            <div className="proposal-actions-block">
              <p>bbox가 준비되었습니다. 필요하면 다시 그리고 3D 제안을 생성하세요.</p>
              <button type="button" className="button primary compact" onClick={() => void manual.submitStagedBbox()}><Check size={13} /> 3D 제안 생성</button>
              <button type="button" className="button secondary compact" onClick={manual.retryBbox}><RotateCcw size={13} /> 다시 그리기</button>
            </div>
          )}
          {proposalState?.status === 'loading' && (
            <div className="proposal-actions-block" role="status">
              <p>
                <LoaderCircle className="spin" size={14} />
                {proposalState.preparingAttempt
                  ? `원본 점군 준비 중 · 자동 재시도 ${proposalState.preparingAttempt}/${proposalState.preparingMaxAttempts}`
                  : 'bbox에서 3D 위치를 계산하고 있습니다.'}
              </p>
              <button type="button" className="button secondary compact" onClick={manual.cancel}>취소</button>
            </div>
          )}
          {proposalState?.status === 'error' && (
            <div className="proposal-error" role="alert">
              <strong><AlertTriangle size={14} /> 제안 실패</strong>
              <span>{proposalState.message}</span>
              <div>
                {proposalState.geometry && <button type="button" className="button primary compact" onClick={() => void manual.retryProposal()}>계산 재시도</button>}
                <button type="button" className="button secondary compact" onClick={manual.retryBbox}>bbox 다시 그리기</button>
                <button type="button" className="button secondary compact" onClick={manual.cancel}>취소</button>
              </div>
            </div>
          )}
          {(proposalState?.status === 'ready' || proposalState?.status === 'committing') && (
            <div className="proposal-ready">
              <div className="proposal-coordinate">
                <span>{proposalState.data.proposal.status === 'review' ? '검토 필요' : '제안 준비'}</span>
                <strong>{position?.map((value) => value.toFixed(3)).join(', ')}</strong>
                {proposalQuality && <small>품질 {Math.round(proposalQuality.score * 100)}% · 지지점 {proposalQuality.support_point_count ?? 0}</small>}
              </div>
              {proposalState.data.proposal.reason_codes.length > 0 && <p className="proposal-warning">{proposalState.data.proposal.reason_codes.join(' · ')}</p>}
              {proposalState.data.duplicate.exact_duplicate && <p className="proposal-blocked">동일 객체가 이미 존재하여 저장할 수 없습니다.</p>}
              {proposalState.data.duplicate.warning_count > 0 && !proposalState.data.duplicate.exact_duplicate && (
                <div className="proposal-duplicate-override">
                  <label><input type="checkbox" checked={manual.allowNearDuplicate} onChange={(event) => manual.setAllowNearDuplicate(event.target.checked)} /> 근접 객체와 별개로 저장</label>
                  <input aria-label="근접 중복 저장 사유" placeholder="저장 사유 (3자 이상)" value={manual.duplicateOverrideReason} onChange={(event) => manual.setDuplicateOverrideReason(event.target.value)} />
                </div>
              )}
              {manual.missingRequiredFields.length > 0 && <p className="proposal-blocked">필수 속성: {manual.missingRequiredFields.join(', ')}</p>}
              {manual.reviewTaskLinkChanged && <p className="proposal-blocked" role="alert">검수 항목이 바뀌었습니다. bbox 수정으로 다시 그리거나 취소한 뒤 현재 항목에서 시작하세요.</p>}
              {proposalState.data.saveError && <p className="proposal-blocked">{proposalState.data.saveError}</p>}
              <div className="proposal-confirm-actions">
                <button type="button" className="button primary compact" disabled={proposalState.status === 'committing' || proposalState.data.duplicate.blocked || manual.missingRequiredFields.length > 0 || manual.reviewTaskLinkChanged} onClick={() => void manual.confirmProposal(false)}>{proposalState.status === 'committing' ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />} 확인</button>
                {manual.canConfirmAndNext && <button type="button" className="button primary compact" disabled={proposalState.status === 'committing' || proposalState.data.duplicate.blocked || manual.missingRequiredFields.length > 0 || manual.reviewTaskLinkChanged} onClick={() => void manual.confirmProposal(true)}>Shift+Enter · 저장 후 다음</button>}
                <button type="button" className="button secondary compact" disabled={proposalState.status === 'committing'} onClick={manual.retryBbox}><RotateCcw size={13} /> bbox 수정</button>
                <button type="button" className="button secondary compact" disabled={proposalState.status === 'committing'} onClick={manual.cancel}><X size={13} /> 취소</button>
              </div>
            </div>
          )}

          {manual.templateId === 'SIGN_SUPPORT_POLE' && poleProposal && poleProposal.status !== 'idle' && (
            <div className="proposal-pole-adapter">
              <strong>지주 하단 P0 제안</strong>
              <span>{poleProposal.status === 'picking' ? '점군에서 몸체 점을 선택하세요.' : poleProposal.status === 'loading' ? '하단점을 계산하고 있습니다.' : poleProposal.status === 'error' ? poleProposal.message : poleProposal.result.status === 'failed' ? '산출 실패' : '결과를 확인하세요.'}</span>
              {poleProposal.status === 'ready' && poleProposal.result.reason_codes.length > 0 && <small>{poleBaseReasonMessages(poleProposal.result.reason_codes).join(' · ')}</small>}
              {poleTemplateValidation?.duplicate.exact_duplicate && (
                <p className="proposal-blocked">동일 지주 객체가 이미 존재하여 저장할 수 없습니다.</p>
              )}
              {poleTemplateValidation &&
                poleTemplateValidation.duplicate.warning_count > 0 &&
                !poleTemplateValidation.duplicate.exact_duplicate && (
                  <div className="proposal-duplicate-override">
                    <label>
                      <input
                        type="checkbox"
                        checked={manual.allowNearDuplicate}
                        onChange={(event) => manual.setAllowNearDuplicate(event.target.checked)}
                      />
                      근접 지주와 별개로 저장
                    </label>
                    <input
                      aria-label="지주 근접 중복 저장 사유"
                      placeholder="저장 사유 (3자 이상)"
                      value={manual.duplicateOverrideReason}
                      onChange={(event) => manual.setDuplicateOverrideReason(event.target.value)}
                    />
                  </div>
                )}
              {poleTemplateValidation && poleTemplateValidation.missingRequiredFields.length > 0 && (
                <p className="proposal-blocked">
                  필수 속성: {poleTemplateValidation.missingRequiredFields.join(', ')}
                </p>
              )}
              {overlay.poleBaseReviewTaskChanged && (
                <p className="proposal-blocked" role="alert">
                  검수 항목이 바뀌었습니다. 현재 항목에서 바닥점을 다시 선택해 주세요.
                </p>
              )}
              <div>
                {poleProposal.status === 'ready' && poleProposal.result.status !== 'failed' && <button type="button" className="button primary compact" disabled={overlay.poleBaseReviewTaskChanged || poleBaseTemplateValidationBlocksSave(poleTemplateValidation)} onClick={() => void overlay.confirmPoleBaseProposal()}>확인</button>}
                {poleProposal.status !== 'picking' &&
                  !(
                    poleProposal.status === 'error' &&
                    poleProposal.reasonCodes.includes('TASK_RESOLUTION_PENDING')
                  ) && <button type="button" className="button secondary compact" onClick={overlay.retryPoleBasePick}>다시 선택</button>}
                <button type="button" className="button secondary compact" onClick={overlay.cancelPoleBaseProposal}>취소</button>
              </div>
            </div>
          )}

          {layer && (
            <section className="proposal-history" aria-label="최근 편집 이력">
              <header>
                <span>최근 편집 이력</span>
                <div>
                  <button type="button" aria-label="최근 편집 실행 취소" title="실행 취소" disabled={!canUndo || historyLoading} onClick={() => void mutateHistory('undo')}><Undo2 size={13} /></button>
                  <button type="button" aria-label="최근 편집 다시 실행" title="다시 실행" disabled={!canRedo || historyLoading} onClick={() => void mutateHistory('redo')}><Redo2 size={13} /></button>
                </div>
              </header>
              {historyError && <p role="alert">{historyError}</p>}
              {!historyError && recentHistory.length === 0 && <p>{historyLoading ? '불러오는 중…' : '편집 이력이 없습니다.'}</p>}
              {recentHistory.length > 0 && <ol>{recentHistory.map((item) => <li key={item.audit_id} className={item.undone ? 'undone' : ''}><span>{item.action}</span><code>{item.feature_id}</code><small>r{item.revision}</small></li>)}</ol>}
            </section>
          )}
        </div>
      )}
    </aside>
  )
}
