import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HEADLINE, HEADLINE_LINES, HeroHeadline } from '../app/components/HeroHeadline';

describe('HeroHeadline', () => {
  it('is a real-text heading in the pixel font, one block per wide-screen line', () => {
    const { container } = render(<HeroHeadline />);
    const h1 = screen.getByRole('heading', { level: 1, name: 'Millisecond Decisions, Any Domain' });
    expect(HEADLINE).toBe('Millisecond Decisions, Any Domain');
    expect(h1.textContent).toBe(HEADLINE);
    expect(h1.className).toContain('font-pixel');
    expect(container.querySelectorAll('svg')).toHaveLength(0);
    expect([...h1.querySelectorAll('span.block')].map((s) => s.textContent)).toEqual(HEADLINE_LINES);
  });

  it('uses only whole-cell sizes (multiples of 10 px)', () => {
    render(<HeroHeadline />);
    const sizes = screen.getByRole('heading', { level: 1 }).className.match(/text-pixel-\d+/g);
    expect(sizes).toEqual(['text-pixel-30', 'text-pixel-40', 'text-pixel-50']);
  });
});
