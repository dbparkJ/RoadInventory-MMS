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
        settings={{ ...DEFAULT_USER_SETTINGS, panoramaPointOverlayEnabled: true }}
        onChange={onChange}
        onReset={onReset}
      />,
    )

    fireEvent.change(screen.getByRole('spinbutton', { name: '파노라마 정면 보정각' }), {
      target: { value: '7.5' },
    })
    fireEvent.click(screen.getByRole('checkbox', { name: '파노라마 포인트 오버레이 표시' }))
    fireEvent.change(screen.getByRole('slider', { name: '파노라마 영상 투명도' }), {
      target: { value: '0.35' },
    })
    fireEvent.change(screen.getByRole('combobox', { name: '파노라마 기본 화질' }), {
      target: { value: 'ultra' },
    })
    expect(screen.getByText('8 cm')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('지주 바닥점 마커 색상'), {
      target: { value: '#ff00aa' },
    })
    fireEvent.change(screen.getByRole('slider', { name: '지주 바닥점 마커 크기' }), {
      target: { value: '0.12' },
    })
    fireEvent.click(screen.getByRole('checkbox', { name: '지도에 모든 트랙 표시' }))
    fireEvent.click(screen.getByRole('button', { name: '기본 설정으로 복원' }))

    expect(onChange).toHaveBeenNthCalledWith(1, { panoramaForwardOffsetDeg: 7.5 })
    expect(onChange).toHaveBeenNthCalledWith(2, { panoramaPointOverlayEnabled: false })
    expect(onChange).toHaveBeenNthCalledWith(3, { panoramaImageOpacity: 0.35 })
    expect(onChange).toHaveBeenNthCalledWith(4, { panoramaDefaultQuality: 'ultra' })
    expect(onChange).toHaveBeenNthCalledWith(5, { poleBaseMarkerColor: '#ff00aa' })
    expect(onChange).toHaveBeenNthCalledWith(6, { poleBaseMarkerSizeM: 0.12 })
    expect(onChange).toHaveBeenNthCalledWith(7, { showAllMapTracks: true })
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

    expect(screen.getByRole('slider', { name: '파노라마 영상 투명도' })).toBeDisabled()
    expect(screen.getByText('포인트는 선명하게 유지하고 배경 영상만 흐리게 조절합니다.')).toBeInTheDocument()
    expect(screen.getByText('활성 트랙만 색상으로 표시합니다.')).toBeInTheDocument()
  })
})
