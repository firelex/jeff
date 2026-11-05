import { beforeAll, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Dialog } from './dialog';

// jsdom does not implement <dialog>'s showModal/close; stand in with the same
// bookkeeping a real browser does (the `open` attribute), so the component's
// own effect logic is what's under test, not the browser API.
beforeAll(() => {
  if (!HTMLDialogElement.prototype.showModal) {
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    };
  }
  if (!HTMLDialogElement.prototype.close) {
    HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
      this.removeAttribute('open');
      this.dispatchEvent(new Event('close'));
    };
  }
});

describe('Dialog', () => {
  it('renders its title and content when open', () => {
    render(
      <Dialog open onClose={() => {}} title="Confirm">
        <p>Are you sure?</p>
      </Dialog>,
    );
    expect(screen.getByText('Confirm')).toBeTruthy();
    expect(screen.getByText('Are you sure?')).toBeTruthy();
    expect(screen.getByRole('dialog').hasAttribute('open')).toBe(true);
  });

  it('is closed when open is false', () => {
    render(
      <Dialog open={false} onClose={() => {}} title="Confirm">
        <p>Are you sure?</p>
      </Dialog>,
    );
    expect(document.querySelector('dialog')?.hasAttribute('open')).toBe(false);
  });

  it('calls onClose when the close button is clicked', async () => {
    const onClose = vi.fn();
    render(
      <Dialog open onClose={onClose} title="Confirm">
        <p>Are you sure?</p>
      </Dialog>,
    );
    screen.getByLabelText('Close').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(onClose).toHaveBeenCalled();
  });
});
