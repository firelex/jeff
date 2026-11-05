import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Button } from './button';

describe('Button', () => {
  it('renders the primary, ghost and pending variants', () => {
    render(
      <>
        <Button variant="primary">Primary</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="pending">Pending</Button>
      </>,
    );
    expect(screen.getByText('Primary').className).toContain('bg-primary');
    expect(screen.getByText('Ghost').className).toContain('border-primary-border');
    expect(screen.getByText('Pending').className).toContain('border-dashed');
  });

  it('disables the button and shows a spinner when pending', () => {
    render(<Button pending>Save</Button>);
    const button = screen.getByRole('button', { name: /save/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByRole('status')).toBeTruthy();
  });

  it('is not disabled and has no spinner when not pending', () => {
    render(<Button>Save</Button>);
    const button = screen.getByRole('button', { name: /save/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('calls onClick when clicked', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Go</Button>);
    await userEvent.click(screen.getByText('Go'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});
