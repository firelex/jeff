import { render, screen } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import Pricing, { loader } from '../app/routes/pricing';

afterEach(() => vi.unstubAllEnvs());

function show(data: { price: string; allowance: string; tax: boolean }) {
  const Stub = createRoutesStub([{ path: '/pricing', Component: Pricing as never, loader: () => data }]);
  render(<Stub initialEntries={['/pricing']} />);
}

it('shows the two columns and the comparison line', async () => {
  show({ price: '$1', allowance: '50 million', tax: false });
  expect(await screen.findByText('Free forever, every adapter, the SDKs and the engine on your machine.')).toBeTruthy();
  expect(screen.getByRole('heading', { level: 2, name: 'Local' })).toBeTruthy();
  expect(screen.getByRole('heading', { level: 2, name: 'API' })).toBeTruthy();
  expect(screen.getByText('$1 a month, 50 million input tokens a month, every adapter, cancel any time.')).toBeTruthy();
  expect(screen.getByText('When the allowance is used up, calls are refused until your next billing period starts: you are never charged more than $1 a month.')).toBeTruthy();
  expect(screen.getByText("Half TypeSafe's price per token at the full allowance.")).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Get an API key' }).getAttribute('href')).toBe('/signup');
  expect(screen.getByRole('link', { name: 'Read the quick start' }).getAttribute('href')).toBe('/docs/guides/quickstart');
});

it('with automatic tax on, says tax may be added to the price', async () => {
  show({ price: '$1', allowance: '50 million', tax: true });
  expect(await screen.findByText('When the allowance is used up, calls are refused until your next billing period starts: you are never charged more than $1 a month, plus any VAT or sales tax.')).toBeTruthy();
});

it('reads the price and the allowance from the plan settings', () => {
  vi.stubEnv('PLAN_PRICE_USD', '2.5');
  vi.stubEnv('PLAN_ALLOWANCE_INPUT_TOKENS', '7000000');
  vi.stubEnv('TURNSTILE_SITE_KEY', 'site');
  vi.stubEnv('PLAYGROUND_RUNS_PER_MINUTE', '5');
  vi.stubEnv('STRIPE_AUTOMATIC_TAX', 'off');
  vi.stubEnv('SIGN_IN_PROVIDERS', 'github,email');
  expect(loader()).toEqual({ price: '$2.50', allowance: '7 million', tax: false });
  vi.stubEnv('STRIPE_AUTOMATIC_TAX', 'on');
  expect(loader()).toEqual({ price: '$2.50', allowance: '7 million', tax: true });
});

it('refuses to build without the plan settings', () => {
  vi.stubEnv('PLAN_PRICE_USD', '');
  vi.stubEnv('PLAN_ALLOWANCE_INPUT_TOKENS', '7000000');
  vi.stubEnv('TURNSTILE_SITE_KEY', 'site');
  vi.stubEnv('PLAYGROUND_RUNS_PER_MINUTE', '5');
  vi.stubEnv('STRIPE_AUTOMATIC_TAX', 'off');
  vi.stubEnv('SIGN_IN_PROVIDERS', 'github,email');
  expect(() => loader()).toThrow('PLAN_PRICE_USD');
  vi.stubEnv('PLAN_PRICE_USD', '1');
  vi.stubEnv('STRIPE_AUTOMATIC_TAX', 'yes');
  expect(() => loader()).toThrow('STRIPE_AUTOMATIC_TAX');
});
