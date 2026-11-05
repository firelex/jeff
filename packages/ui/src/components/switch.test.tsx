import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Switch } from './switch';

describe('Switch', () => {
  it('renders the checked and unchecked state', () => {
    render(
      <>
        <Switch checked onChange={() => {}} aria-label="on" />
        <Switch checked={false} onChange={() => {}} aria-label="off" />
      </>,
    );
    expect(screen.getByLabelText('on').getAttribute('aria-checked')).toBe('true');
    expect(screen.getByLabelText('off').getAttribute('aria-checked')).toBe('false');
  });

  it('calls onChange with the flipped value when clicked', async () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} aria-label="toggle" />);
    await userEvent.click(screen.getByLabelText('toggle'));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('does not call onChange when disabled', async () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onChange={onChange} disabled aria-label="toggle" />);
    await userEvent.click(screen.getByLabelText('toggle'));
    expect(onChange).not.toHaveBeenCalled();
  });
});
