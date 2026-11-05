import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatusDot } from './status-dot';
import type { Status } from './status-dot';

const EXPECTED: Record<Status, string> = {
  ready: 'bg-success',
  degraded: 'bg-warning',
  broken: 'bg-danger',
  unknown: 'bg-faint',
};

describe('StatusDot', () => {
  it('maps each status to its own colour class', () => {
    for (const status of Object.keys(EXPECTED) as Status[]) {
      render(<StatusDot status={status} label={status} />);
      const dot = screen.getByTestId(`status-dot-${status}`);
      expect(dot.className).toContain(EXPECTED[status]);
    }
  });
});
