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
        settings={DEFAULT_USER_SETTINGS}
        onChange={onChange}
        onReset={onReset}
      />,
    )

    fireEvent.change(screen.getByRole('spinbutton', { name: '파노라마 정면 보정각' }), {
      target: { value: '7.5' },
    })
    fireEvent.change(screen.getByRole('slider', { name: '파노라마 검출 표시 거리' }), {
      target: { value: '80' },
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
    fireEvent.click(screen.getByRole('button', { name: '기본 설정으로 복원' }))

    expect(onChange).toHaveBeenNthCalledWith(1, { panoramaForwardOffsetDeg: 7.5 })
    expect(onChange).toHaveBeenNthCalledWith(2, { detectionVisibilityDistanceM: 80 })
    expect(onChange).toHaveBeenNthCalledWith(3, { panoramaDefaultQuality: 'ultra' })
    expect(onChange).toHaveBeenNthCalledWith(4, { poleBaseMarkerColor: '#ff00aa' })
    expect(onChange).toHaveBeenNthCalledWith(5, { poleBaseMarkerSizeM: 0.12 })
    expect(onChange).toHaveBeenCalledTimes(5)
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it('does not expose the removed panorama point overlay controls', () => {
    render(
      <GeneralSettingsPanel
        settings={DEFAULT_USER_SETTINGS}
        onChange={vi.fn()}
        onReset={vi.fn()}
      />,
    )

    expect(screen.queryByRole('checkbox', { name: '파노라마 포인트 오버레이 표시' })).not.toBeInTheDocument()
    expect(screen.queryByRole('slider', { name: '파노라마 영상 투명도' })).not.toBeInTheDocument()
    expect(screen.getByRole('slider', { name: '파노라마 검출 표시 거리' })).toBeEnabled()
    expect(screen.queryByRole('checkbox', { name: '지도에 모든 트랙 표시' })).not.toBeInTheDocument()
  })
})
