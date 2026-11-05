import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ErrorBoundary } from './ErrorBoundary';

function Thrower(): never {
  throw new TypeError('jeff.x is not a function ID of the form jeff.<area>.<adapter>.<function>');
}

describe('ErrorBoundary', () => {
  it('shows a render-time throw as an alert with its name and message, and starts again when its key changes', async () => {
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {});
    function Pages() {
      const [page, setPage] = useState('broken');
      return (
        <>
          <button type="button" onClick={() => setPage('fine')}>Go</button>
          <ErrorBoundary key={page}>{page === 'broken' ? <Thrower /> : <p>The other page</p>}</ErrorBoundary>
        </>
      );
    }
    render(<Pages />);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('TypeError: jeff.x is not a function ID');
    await userEvent.setup().click(screen.getByRole('button', { name: 'Go' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('The other page')).toBeTruthy();
    quiet.mockRestore();
  });
});
