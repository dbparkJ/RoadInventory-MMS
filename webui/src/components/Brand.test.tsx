import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Brand, BrandLogo } from './Brand'

describe('Brand', () => {
  it('keeps the product title text-only and exposes the GEO& bitmap as a separate mark', () => {
    const { container } = render(
      <>
        <Brand />
        <BrandLogo />
      </>,
    )

    expect(screen.getByText('MMS 도로대장 자동화')).toBeInTheDocument()
    expect(screen.getByText('ROAD INVENTORY WORKSPACE')).toBeInTheDocument()
    expect(container.querySelector('.brand')).not.toContainElement(
      screen.getByRole('img', { name: 'GEO&' }),
    )
    expect(container.querySelector('.brand-signature')).toContainElement(
      screen.getByRole('img', { name: 'GEO&' }),
    )
    expect(screen.getByRole('img', { name: 'GEO&' })).toHaveAttribute(
      'src',
      expect.stringContaining('logo.png'),
    )
    expect(screen.queryByText('MMS Studio')).not.toBeInTheDocument()
  })
})
