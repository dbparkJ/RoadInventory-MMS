import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DEFAULT_USER_SETTINGS } from '../lib/userSettings'
import { GeneralSettingsPanel } from './GeneralSettingsPanel'

afterEach(cleanup)

describe('GeneralSettingsPanel', () => {
  it('exposes accessible controls and reports setting patches', () => {
    const onChange = vi.fn()
    const onReset = vi.fn()

    render(
      <GeneralSettingsPanel
        settings={{ ...DEFAULT_USER_SETTINGS }}
        onChange={onChange}
        onReset={onReset}
      />,
    )

    fireEvent.change(screen.getByRole('spinbutton', { name: '파노라마 정면 보정각' }), {
      target: { value: '7.5' },
    })
    fireEvent.click(screen.getByRole('checkbox', { name: '파노라마 포인트 오버레이 표시' }))
    fireEvent.change(screen.getByRole('combobox', { name: '파노라마 기본 화질' }), {
      target: { value: 'ultra' },
    })
    fireEvent.click(screen.getByRole('checkbox', { name: '지도에 모든 트랙 표시' }))
    fireEvent.click(screen.getByRole('button', { name: '기본 설정으로 복원' }))

    expect(onChange).toHaveBeenNthCalledWith(1, { panoramaForwardOffsetDeg: 7.5 })
    expect(onChange).toHaveBeenNthCalledWith(2, { panoramaPointOverlayEnabled: true })
    expect(onChange).toHaveBeenNthCalledWith(3, { panoramaDefaultQuality: 'ultra' })
    expect(onChange).toHaveBeenNthCalledWith(4, { showAllMapTracks: true })
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it('disables opacity while the point overlay is hidden', () => {
    render(
      <GeneralSettingsPanel
        settings={{ ...DEFAULT_USER_SETTINGS, panoramaPointOverlayEnabled: false }}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.getByRole('slider', { name: '파노라마 포인트 투명도' })).toBeDisabled()
    expect(screen.getByText('활성 트랙만 색상으로 표시합니다.')).toBeInTheDocument()
  })
})
