import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EmptyState } from './empty-state';

describe('EmptyState', () => {
  it('renders a title, a description and an action', () => {
    render(<EmptyState title="No adapters" description="Train one to see it here." action={<button>Train</button>} />);
    expect(screen.getByText('No adapters')).toBeTruthy();
    expect(screen.getByText('Train one to see it here.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Train' })).toBeTruthy();
  });
});
