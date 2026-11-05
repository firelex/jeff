import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LoginForm } from './login-form';

describe('LoginForm', () => {
  it('submits a plain login name, not only an email address', async () => {
    const onSubmit = vi.fn();
    render(<LoginForm onSubmit={onSubmit} />);
    expect((screen.getByLabelText('Login') as HTMLInputElement).type).toBe('text');
    await userEvent.type(screen.getByLabelText('Login'), 'mathias');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    expect(onSubmit).toHaveBeenCalledWith('mathias', 'secret');
  });

  it('submits the entered login and password', async () => {
    const onSubmit = vi.fn();
    render(<LoginForm onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText('Login'), 'a@b.com');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
    expect(onSubmit).toHaveBeenCalledWith('a@b.com', 'secret');
  });

  it('shows a passed error', () => {
    render(<LoginForm onSubmit={() => {}} error={{ type: 'unauthorized', message: 'Wrong email or password.' }} />);
    expect(screen.getByText('unauthorized')).toBeTruthy();
    expect(screen.getByText('Wrong email or password.')).toBeTruthy();
  });

  it('shows nothing when there is no error', () => {
    render(<LoginForm onSubmit={() => {}} />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('two forms on one page each label their own fields (ids from useId, never fixed)', () => {
    render(
      <>
        <LoginForm onSubmit={() => {}} />
        <LoginForm onSubmit={() => {}} />
      </>,
    );
    const emails = screen.getAllByLabelText('Login');
    expect(emails.length).toBe(2);
    expect(emails[0].id).not.toBe(emails[1].id);
    expect(emails[0].id).not.toMatch(/^jeff-console-/);
  });
});
