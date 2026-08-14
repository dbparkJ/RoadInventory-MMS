import { Flag, RotateCcw } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { Frame, FrameRange } from '../types'

export function FrameRangePicker({
  frameLimit,
  selectedFrame,
  frameRange,
  onSetStart,
  onSetEnd,
  onChange,
  onClear,
}: {
  frameLimit: number
  selectedFrame: Frame | null
  frameRange: FrameRange | null
  onSetStart: (ordinal: number) => void
  onSetEnd: (ordinal: number) => void
  onChange: (range: FrameRange) => void
  onClear: () => void
}) {
  const safeLimit = Math.max(1, frameLimit)
  const [draft, setDraft] = useState<[string, string]>(['', ''])
  const parsed = draft.map((value) => Number(value)) as [number, number]
  const valid = parsed.every(
    (value) => Number.isInteger(value) && value >= 1 && value <= safeLimit,
  )

  useEffect(() => {
    setDraft(frameRange ? [String(frameRange[0] + 1), String(frameRange[1] + 1)] : ['', ''])
  }, [frameRange, safeLimit])

  const apply = () => {
    if (!valid) return
    onChange([
      Math.min(parsed[0], parsed[1]) - 1,
      Math.max(parsed[0], parsed[1]) - 1,
    ])
  }

  return (
    <section className="setup-section detection-frame-range" aria-label="실행 프레임 범위">
      <div className="section-label">
        <span>실행 프레임 범위</span>
        <small>지도에서는 A/D 키로 현재 프레임 이동</small>
      </div>
      <div className="frame-range-picker compact">
        <div className="frame-range-inputs">
          <label>
            <span>시작 프레임</span>
            <input
              type="number"
              min={1}
              max={safeLimit}
              value={draft[0]}
              placeholder="1"
              aria-label="실행 시작 프레임 번호"
              onChange={(event) => setDraft((current) => [event.target.value, current[1]])}
              onKeyDown={(event) => event.key === 'Enter' && apply()}
            />
          </label>
          <span aria-hidden="true">–</span>
          <label>
            <span>끝 프레임</span>
            <input
              type="number"
              min={1}
              max={safeLimit}
              value={draft[1]}
              placeholder={String(safeLimit)}
              aria-label="실행 끝 프레임 번호"
              onChange={(event) => setDraft((current) => [current[0], event.target.value])}
              onKeyDown={(event) => event.key === 'Enter' && apply()}
            />
          </label>
          <button type="button" disabled={!valid} onClick={apply}>적용</button>
        </div>
        <div className="frame-range-actions" role="group" aria-label="실행 프레임 범위 지정">
          <button
            type="button"
            disabled={!selectedFrame}
            aria-pressed={Boolean(selectedFrame && frameRange?.[0] === selectedFrame.index)}
            onClick={() => selectedFrame && onSetStart(selectedFrame.index)}
          >
            시작 지정
          </button>
          <button
            type="button"
            disabled={!selectedFrame}
            aria-pressed={Boolean(selectedFrame && frameRange?.[1] === selectedFrame.index)}
            onClick={() => selectedFrame && onSetEnd(selectedFrame.index)}
          >
            끝 지정
          </button>
          <button type="button" disabled={!frameRange} onClick={onClear}>
            <RotateCcw size={11} /> 전체
          </button>
        </div>
        <div className="frame-range-row" aria-live="polite">
          <Flag size={13} />
          <span>
            <small>실행 범위</small>
            <strong>
              {frameRange
                ? `Frame ${String(frameRange[0] + 1).padStart(4, '0')}–${String(
                    frameRange[1] + 1,
                  ).padStart(4, '0')}`
                : '현재 작업 구간 전체'}
            </strong>
          </span>
          <code>{frameRange ? `ordinal ${frameRange[0]}–${frameRange[1]}` : 'ALL'}</code>
        </div>
      </div>
    </section>
  )
}
