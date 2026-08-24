import { AlertTriangle, BookmarkPlus, ChevronLeft, ChevronRight, ClipboardCheck, MapPinned, RotateCcw, SkipForward } from 'lucide-react'
import type { ReviewTaskStatus, ReviewTaskType } from '../types'
import { isReviewTaskComplete, useOptionalReviewWorkspace } from './ReviewContext'
import { ReviewTaskCard } from './ReviewTaskCard'

export function ReviewQueue() {
  const review = useOptionalReviewWorkspace()
  if (!review?.enabled || !review.queueOpen || !review.datasetId) return null

  const currentComplete = review.currentTask ? isReviewTaskComplete(review.currentTask) : false
  const busy = Boolean(
    review.updatingTaskId || review.creatingSession || review.generatingCandidates,
  )
  const taskActionsEnabled = review.session?.status === 'active'

  return (
    <aside className="review-queue" aria-label="검수 항목 목록">
      <header className="review-queue-header">
        <span>
          <ClipboardCheck size={15} />
          <strong>검수할 항목</strong>
          <small>{review.totalCount.toLocaleString('ko-KR')}</small>
        </span>
        <div>
          <button
            type="button"
            aria-label="이전 검수 항목"
            title="이전 검수 항목 (K)"
            disabled={review.currentTaskIndex <= 0 || busy}
            onClick={() => review.moveTask(-1)}
          >
            <ChevronLeft size={14} />
          </button>
          <button
            type="button"
            aria-label="다음 검수 항목"
            title="다음 검수 항목 (J)"
            disabled={
              review.currentTaskIndex < 0 ||
              (review.currentTaskIndex >= review.tasks.length - 1 && !review.hasMoreTasks) ||
              busy
            }
            onClick={() => review.moveTask(1)}
          >
            <ChevronRight size={14} />
          </button>
          <button
            type="button"
            aria-label="검수 작업 큐 닫기"
            title="검수 작업 큐 닫기"
            onClick={() => review.setQueueOpen(false)}
          >
            ×
          </button>
        </div>
      </header>

      <div className="review-queue-filters">
        <select
          aria-label="검수 작업 상태 필터"
          value={review.taskStatusFilter}
          onChange={(event) => review.setTaskStatusFilter(event.target.value as ReviewTaskStatus | '')}
        >
          <option value="">모든 상태</option>
          <option value="todo">대기</option>
          <option value="in_progress">검수 중</option>
          <option value="confirmed">완료</option>
          <option value="corrected">수정 완료</option>
          <option value="manual_added">수동 추가</option>
          <option value="false_positive">오검출</option>
          <option value="skipped">건너뜀</option>
          <option value="field_survey">현장조사</option>
        </select>
        <select
          aria-label="검수 후보 유형 필터"
          value={review.taskTypeFilter}
          onChange={(event) => review.setTaskTypeFilter(event.target.value as ReviewTaskType | '')}
        >
          <option value="">모든 후보 유형</option>
          <option value="MANUAL_SCAN">수동 확인</option>
          <option value="LOW_CONFIDENCE">낮은 신뢰도</option>
          <option value="PROJECTION_FAILED">3D 위치화 실패</option>
          <option value="GEOMETRY_REVIEW">형상 재검토</option>
          <option value="POLE_BASE_REVIEW">지주 하단 재검토</option>
          <option value="SPACING_ANOMALY">간격 이상</option>
          <option value="UNREVIEWED_INTERVAL">미검수 구간</option>
          <option value="MANUAL_FLAG">나중에 확인</option>
        </select>
      </div>

      {review.loading && review.tasks.length === 0 ? (
        <p className="review-queue-message">검수 작업을 불러오는 중입니다.</p>
      ) : review.error && review.tasks.length === 0 ? (
        <p className="review-queue-message error" role="alert">{review.error}</p>
      ) : !review.session ? (
        <div className="review-queue-message">
          <p>아직 시작한 검수 작업이 없습니다.</p>
          <small>범위와 후보 유형을 고르면 확인할 항목을 자동으로 준비합니다.</small>
          <button type="button" className="button primary compact" onClick={() => review.setStartGuideOpen(true)}>
            새 검수 작업 시작
          </button>
        </div>
      ) : review.tasks.length === 0 ? (
        <div className="review-queue-message">
          <p>이 작업에 만들어진 후보가 없습니다.</p>
          <small>후보를 추가하거나, 확인할 후보가 없다면 QA 검사를 실행한 뒤 작업을 완료할 수 있습니다.</small>
          {review.session.status === 'active' && (
            <button type="button" className="button primary compact" disabled={busy} onClick={() => review.setCandidateGuideOpen(true)}>
              검수 후보 만들기
            </button>
          )}
        </div>
      ) : (
        <div className="review-task-list">
          {review.tasks.map((task) => (
            <ReviewTaskCard
              key={task.id}
              task={task}
              current={task.id === review.currentTask?.id}
              busy={busy}
              onSelect={() => review.selectTask(task.id)}
            />
          ))}
          {review.hasMoreTasks && (
            <button type="button" className="button secondary compact review-load-more" disabled={busy} onClick={() => void review.loadMoreTasks()}>
              다음 200개 불러오기
            </button>
          )}
        </div>
      )}

      {review.error && review.tasks.length > 0 && (
        <p className="review-queue-inline-error" role="alert">{review.error}</p>
      )}

      {review.currentTask && (
        <footer className="review-task-actions">
          {currentComplete ? (
            <button type="button" className="button secondary compact" disabled={busy || !taskActionsEnabled} onClick={() => void review.reopenCurrent()}>
              <RotateCcw size={13} /> 다시 확인
            </button>
          ) : <button
            type="button"
            className="button secondary compact"
            disabled={busy || !taskActionsEnabled || review.currentTask.status !== 'in_progress'}
            onClick={() => void review.resolveCurrent('skipped')}
          >
            <SkipForward size={13} /> 건너뛰기
          </button>}
          <button
            type="button"
            className="button secondary compact"
            disabled={busy || !taskActionsEnabled || review.currentTask.status !== 'in_progress'}
            onClick={() => void review.resolveCurrent('field_survey')}
          >
            <MapPinned size={13} /> 현장조사
          </button>
          {!currentComplete && <button
            type="button"
            className="button secondary compact"
            disabled={busy || !taskActionsEnabled || review.currentTask.status !== 'in_progress'}
            title="오검출 처리 (X)"
            onClick={() => void review.resolveCurrent('false_positive')}
          >
            <AlertTriangle size={13} /> 오검출
          </button>}
          <button
            type="button"
            className="button primary compact"
            disabled={busy || !taskActionsEnabled || review.currentTask.status !== 'in_progress'}
            onClick={() => void review.resolveCurrent('confirmed')}
          >
            <ClipboardCheck size={13} /> 완료
          </button>
          <button
            type="button"
            className="button secondary compact"
            disabled={review.generatingCandidates || !taskActionsEnabled}
            title="현재 프레임을 나중에 확인 (F)"
            onClick={() => void review.flagCurrentFrame(review.currentTask?.target_layer_id ?? undefined)}
          >
            <BookmarkPlus size={13} /> 나중에 확인
          </button>
        </footer>
      )}
    </aside>
  )
}
