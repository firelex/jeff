import { render, screen } from '@testing-library/react';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router';
import { expect, it, vi } from 'vitest';

type Session = { data: null | { user: { name: string; email: string; image: string | null } }; isPending: boolean;
  error: null | { message?: string; status: number; statusText: string } };
const session = vi.hoisted(() => ({ value: { data: null, isPending: false, error: null } as Session }));
const made = vi.hoisted(() => ({ count: 0 }));
vi.mock('../app/lib/auth-client', () => ({ authClient: () => { made.count += 1; return { useSession: () => session.value }; } }));

const { AccountButtons } = await import('../app/components/AccountButtons');

it('a visitor sees Sign in and Get an API key', () => {
  session.value = { data: null, isPending: false, error: null };
  render(<MemoryRouter><AccountButtons /></MemoryRouter>);
  expect(screen.getByRole('link', { name: 'Sign in' }).getAttribute('href')).toBe('/signin');
  expect(screen.getByRole('link', { name: 'Get an API key' }).getAttribute('href')).toBe('/signup');
});

it('a signed-in user sees their avatar, linking to the account page', () => {
  session.value = { data: { user: { name: 'Ada', email: 'ada@example.com', image: null } }, isPending: false, error: null };
  render(<MemoryRouter><AccountButtons /></MemoryRouter>);
  const avatar = screen.getByRole('link', { name: 'Your account (Ada)' });
  expect(avatar.getAttribute('href')).toBe('/account');
  expect(avatar.textContent).toBe('A');
  expect(screen.queryByRole('link', { name: 'Sign in' })).toBeNull();
  expect(screen.queryByRole('link', { name: 'Get an API key' })).toBeNull();
});

it('an account made by an email link has no name: its email names it', () => {
  session.value = { data: { user: { name: '', email: 'bo@example.com', image: null } }, isPending: false, error: null };
  render(<MemoryRouter><AccountButtons /></MemoryRouter>);
  expect(screen.getByRole('link', { name: 'Your account (bo@example.com)' }).textContent).toBe('B');
});

it('while the session is not known yet it holds the place without a wrong button', () => {
  session.value = { data: null, isPending: true, error: null };
  render(<MemoryRouter><AccountButtons /></MemoryRouter>);
  expect(screen.queryByRole('link', { name: 'Sign in' })).toBeNull();
  expect(screen.queryByRole('link', { name: /Your account/ })).toBeNull();
  expect(screen.queryAllByRole('link')).toHaveLength(0);
});

it('the prerendered markup asks for no session and holds the place, so hydration matches it', () => {
  made.count = 0;
  session.value = { data: { user: { name: 'Ada', email: 'ada@example.com', image: null } }, isPending: false, error: null };
  const html = renderToString(<MemoryRouter><AccountButtons /></MemoryRouter>);
  expect(made.count).toBe(0);
  expect(html).not.toContain('<a');
  expect(html).toContain('aria-hidden="true"');
});

it('when the session cannot be checked, it says so once in the console and links to the account page, which finds out', () => {
  const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
  const error = { message: 'Failed to fetch', status: 0, statusText: '' };
  session.value = { data: null, isPending: false, error };
  const { rerender } = render(<MemoryRouter><AccountButtons /></MemoryRouter>);
  rerender(<MemoryRouter><AccountButtons /></MemoryRouter>);
  const link = screen.getByRole('link', { name: 'Account' });
  expect(link.getAttribute('href')).toBe('/account');
  expect(link.getAttribute('title')).toBe('Could not check whether you are signed in: Failed to fetch');
  expect(screen.queryByRole('link', { name: 'Sign in' })).toBeNull();
  expect(logged).toHaveBeenCalledTimes(1);
  expect(logged).toHaveBeenCalledWith('Could not check whether you are signed in: Failed to fetch', error);
  logged.mockRestore();
});

it.each([
  [{ message: '', status: 503, statusText: 'Service Unavailable' }, 'the hub answered 503 Service Unavailable'],
  [{ status: 500, statusText: '' }, 'the hub answered 500'],
  [{ message: '  ', status: 0, statusText: '' }, 'the request did not reach the hub'],
])('a failure with no message (%o) is named by its status, never ending on a colon', (error, words) => {
  const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
  session.value = { data: null, isPending: false, error };
  render(<MemoryRouter><AccountButtons /></MemoryRouter>);
  expect(screen.getByRole('link', { name: 'Account' }).getAttribute('title')).toBe(`Could not check whether you are signed in: ${words}`);
  logged.mockRestore();
});
