import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Spinner } from './spinner';

describe('Spinner', () => {
  it('renders a status role for assistive tech', () => {
    render(<Spinner />);
    expect(screen.getByRole('status')).toBeTruthy();
  });
});
