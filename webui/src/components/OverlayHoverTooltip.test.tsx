import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  OVERLAY_DETAILS_EVENT,
  OverlayHoverTooltip,
  clampOverlayTooltipPosition,
  openOverlayFeatureDetails,
  overlayHoverClassName,
  overlayHoverLayerColor,
  overlayPropertyEntries,
  overlayPropertyPreviewEntries,
} from './OverlayHoverTooltip'

afterEach(cleanup)

describe('OverlayHoverTooltip', () => {
  it('clamps a dragged card inside its viewport with padding', () => {
    expect(clampOverlayTooltipPosition(-40, 400, 100, 90, 320, 240)).toEqual({
      left: 8,
      top: 142,
    })
    expect(clampOverlayTooltipPosition(40, 50, 400, 300, 320, 240)).toEqual({
      left: 8,
      top: 8,
    })
  })

  it('uses the detected class as the primary title and shows a compact colored layer badge', () => {
    const { container } = render(
      <OverlayHoverTooltip
        hover={{
          layerName: 'traffic-signs',
          featureId: 'f-7',
          properties: { class_nm: 'warning-sign', __overlay_color: '#b58b36' },
          x: 10,
          y: 20,
          viewportWidth: 800,
          viewportHeight: 600,
        }}
      />,
    )

    expect(container.querySelector('.overlay-hover-title strong')).toHaveTextContent('warning-sign')
    expect(container.querySelector('.overlay-hover-layer')).toHaveTextContent('traffic-signs')
    expect(container.querySelector('.overlay-hover-layer i')).toHaveStyle({
      backgroundColor: '#b58b36',
    })
  })

  it('uses an explicit raw YOLO model color without a registered SHP layer', () => {
    const { container } = render(
      <OverlayHoverTooltip
        hover={{
          layerName: 'YOLO · traffic-sign.pt',
          layerColor: '#ffb84d',
          featureId: 'det-7',
          properties: { class_nm: 'traffic_sign', conf: 0.91 },
          x: 10,
          y: 20,
          viewportWidth: 800,
          viewportHeight: 600,
        }}
      />,
    )
    expect(container.querySelector('.overlay-hover-title strong')).toHaveTextContent('traffic_sign')
    expect(screen.getByRole('tooltip')).toHaveTextContent('conf')
    expect(screen.getByRole('tooltip')).toHaveTextContent('0.91')
    expect(container.querySelector('.overlay-hover-layer')).toHaveTextContent(
      'YOLO · traffic-sign.pt',
    )
    expect(container.querySelector('.overlay-hover-layer i')).toHaveStyle({
      backgroundColor: '#ffb84d',
    })
  })

  it('resolves class aliases and rejects unsafe renderer colors', () => {
    expect(overlayHoverClassName({ CLASS_NAME: 'pole' }, 9)).toBe('pole')
    expect(overlayHoverClassName({}, 9)).toBe('피처 #9')
    expect(overlayHoverLayerColor({}, '#123abc')).toBe('#123abc')
    expect(overlayHoverLayerColor({ __overlay_color: 'url(bad)' })).toBe('#78909f')
  })

  it('shows layer properties while hiding renderer metadata', () => {
    render(
      <OverlayHoverTooltip
        hover={{
          layerName: '표지판',
          featureId: 'f-7',
          properties: { class_nm: '주의', conf: 0.91, __overlay_color: '#fff' },
          x: 10,
          y: 20,
          viewportWidth: 800,
          viewportHeight: 600,
        }}
      />,
    )
    expect(screen.getByRole('tooltip')).toHaveTextContent('표지판')
    expect(screen.getByRole('tooltip')).toHaveTextContent('class_nm')
    expect(screen.getByRole('tooltip')).not.toHaveTextContent('__overlay_color')
  })

  it('removes every private overlay property', () => {
    expect(overlayPropertyEntries({ value: 1, __overlay_layer_id: 'x' })).toEqual([['value', 1]])
  })

  it('shows only a compact non-empty preview and exposes pinned details actions', () => {
    const onClose = vi.fn()
    const onDetails = vi.fn()
    const hover = {
      layerId: 'layer-1',
      layerName: '표지판',
      featureId: 'f-7',
      properties: { empty: null, a: 1, b: 2, c: 3, d: 4, e: 5 },
      x: 10,
      y: 20,
      viewportWidth: 800,
      viewportHeight: 600,
    }
    render(
      <OverlayHoverTooltip
        hover={hover}
        pinned
        onClose={onClose}
        onDetails={onDetails}
      />,
    )

    expect(screen.getByRole('dialog')).toHaveTextContent('속성 2개 더 있음')
    expect(screen.getByRole('dialog')).not.toHaveTextContent('empty')
    fireEvent.click(screen.getByRole('button', { name: '자세히' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('empty')
    expect(screen.getByRole('dialog')).not.toHaveTextContent('더 있음')
    fireEvent.click(screen.getByRole('button', { name: '수정하기' }))
    fireEvent.click(screen.getByRole('button', { name: '고정 속성 닫기' }))
    expect(onDetails).toHaveBeenCalledWith(hover)
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('prioritizes populated properties in the preview', () => {
    expect(overlayPropertyPreviewEntries({ first: null, second: '값', third: '' }, 2)).toEqual([
      ['second', '값'],
      ['first', null],
    ])
  })

  it('keeps class and confidence visible for detection layers with many fields', () => {
    expect(overlayPropertyPreviewEntries({
      class_id: 65,
      class_nm: '22000',
      model_nm: 'traffic_sign.pt',
      obj_type: 'traffic_sign',
      conf: 0.93,
      bbox_l: 100,
    })).toEqual([
      ['class_nm', '22000'],
      ['obj_type', 'traffic_sign'],
      ['conf', 0.93],
      ['class_id', 65],
    ])
  })

  it('opens the exact layer and feature from a pinned preview', () => {
    const listener = vi.fn()
    window.addEventListener(OVERLAY_DETAILS_EVENT, listener)

    openOverlayFeatureDetails('dataset-1', {
      layerId: 'layer-2',
      featureId: 'feature-9',
    })

    expect(listener).toHaveBeenCalledOnce()
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({
      datasetId: 'dataset-1',
      layerId: 'layer-2',
      featureId: 'feature-9',
    })
    window.removeEventListener(OVERLAY_DETAILS_EVENT, listener)
  })

  it('keeps a pinned preview for inside actions and closes it for outside clicks or Escape', () => {
    const onClose = vi.fn()
    const hover = {
      layerId: 'layer-1',
      layerName: '표지판',
      featureId: 'f-7',
      properties: { class_nm: '주의' },
      x: 10,
      y: 20,
      viewportWidth: 800,
      viewportHeight: 600,
    }
    const { rerender } = render(
      <div>
        <button type="button">다른 도구</button>
        <OverlayHoverTooltip hover={hover} pinned onClose={onClose} />
      </div>,
    )

    fireEvent.pointerDown(screen.getByRole('dialog'))
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.pointerDown(screen.getByRole('button', { name: '다른 도구' }))
    expect(onClose).toHaveBeenCalledOnce()

    onClose.mockClear()
    rerender(<OverlayHoverTooltip hover={hover} pinned onClose={onClose} />)
    expect(screen.getByRole('button', { name: '자세히' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '수정하기' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '수정하기' })).toHaveAttribute(
      'title',
      '편집 가능한 결과 피처가 연결되지 않았습니다.',
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('drags a pinned card by its header and clamps it to the offset container', () => {
    const { container } = render(
      <div>
        <OverlayHoverTooltip
          hover={{
            identityKey: 'detection:7',
            layerName: 'traffic-signs',
            featureId: 'f-7',
            properties: { class_nm: 'warning-sign' },
            x: 10,
            y: 20,
            viewportWidth: 320,
            viewportHeight: 240,
          }}
          pinned
        />
      </div>,
    )
    const offsetParent = container.firstElementChild as HTMLElement
    const dialog = screen.getByRole('dialog')
    const header = dialog.querySelector('header') as HTMLElement
    Object.defineProperty(dialog, 'offsetParent', { configurable: true, value: offsetParent })
    Object.defineProperty(offsetParent, 'clientWidth', { configurable: true, value: 320 })
    Object.defineProperty(offsetParent, 'clientHeight', { configurable: true, value: 240 })
    vi.spyOn(offsetParent, 'getBoundingClientRect').mockReturnValue({
      left: 20,
      top: 30,
    } as DOMRect)
    vi.spyOn(dialog, 'getBoundingClientRect').mockReturnValue({
      left: 40,
      top: 50,
      width: 100,
      height: 100,
    } as DOMRect)
    const dispatchPointer = (
      type: 'pointerdown' | 'pointermove' | 'pointerup',
      values: Record<string, number>,
    ) => {
      const event = new Event(type, { bubbles: true, cancelable: true })
      Object.entries(values).forEach(([key, value]) => {
        Object.defineProperty(event, key, { configurable: true, value })
      })
      fireEvent(header, event)
    }

    dispatchPointer('pointerdown', { pointerId: 7, button: 0, clientX: 50, clientY: 60 })
    expect(dialog).toHaveClass('dragging')
    dispatchPointer('pointermove', { pointerId: 7, clientX: 500, clientY: 500 })
    expect(dialog).toHaveStyle({ left: '212px', top: '132px' })
    dispatchPointer('pointerup', { pointerId: 7, clientX: 500, clientY: 500 })
    expect(dialog).not.toHaveClass('dragging')
  })
})
