import fs from 'node:fs';
import path from 'node:path';
import { test as withoutErrorCheck } from '@playwright/test';
import { expect, test } from './fixtures';

// The security headers (vercel.json, served as Vercel does by scripts/local-server.ts) and every page's script
// policy (scripts/csp.mjs): every page the build has loads and hydrates with no Content-Security-Policy violation
// and no console error (e2e/fixtures.ts), and every answer, the function's included, says it may not be framed.

const CLIENT_DIR = path.join(import.meta.dirname, '..', 'build', 'client');

function builtPages(): string[] {
  return fs.readdirSync(CLIENT_DIR, { recursive: true }).map(String).filter((name) => name.endsWith('.html') && name !== '404.html')
    .map((name) => `/${name.replace(/(^|\/)index\.html$/, '').replace(/\.html$/, '')}`.replace(/\/$/, '') || '/').sort();
}

test('every page loads and hydrates with no Content-Security-Policy violation', async ({ page, allowFailedLoad }) => {
  test.setTimeout(240_000);
  // /account and /account/return ask the function for the visitor's account and subscription; signed out here, the
  // answer is a 401 (and the page goes on to /signin).
  allowFailedLoad('/api/account');
  allowFailedLoad('/api/account/status');
  // /request-adapter asks for the visitor's requests (a 401 signed out), and the owner's pages /admin/adapters and
  // /admin/requests ask for theirs (a 404 to anyone but the owner); each then goes on to sign-in.
  allowFailedLoad('/api/adapter-requests');
  allowFailedLoad('/api/admin/adapters');
  allowFailedLoad('/api/admin/adapter-requests');
  await page.route('https://challenges.cloudflare.com/**', (route) => route.fulfill({ contentType: 'text/javascript',
    body: 'window.turnstile = { render: () => "w", remove: () => {} };' }));
  await page.addInitScript(() => {
    (window as unknown as { violations: string[] }).violations = [];
    document.addEventListener('securitypolicyviolation', (event) => {
      (window as unknown as { violations: string[] }).violations.push(`${event.violatedDirective} ${event.blockedURI} ${event.sample}`);
    });
  });
  // Every path the build prerendered (react-router.config.ts writes allPaths().length to this file), each one a page here.
  const pages = builtPages();
  const { count } = JSON.parse(fs.readFileSync(path.join(CLIENT_DIR, '..', 'prerendered-path-count.json'), 'utf8')) as { count: number };
  expect(pages.length).toBe(count);
  for (const url of pages) {
    const response = await page.goto(url);
    expect(response?.status(), url).toBe(200);
    expect(response?.headers()['content-security-policy'], url).toContain("frame-ancestors 'none'");
    await page.waitForLoadState('networkidle');
    expect(await page.evaluate(() => (window as unknown as { violations: string[] }).violations), url).toEqual([]);
  }
});

// The one check that expects a console error (the browser's report of the refused script), so it runs without the
// fixture's no-console-error rule and checks that error itself.
withoutErrorCheck('an inline script the build did not write is refused', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  const ran = await page.evaluate(async () => {
    const seen = new Promise<string>((resolve) => document.addEventListener('securitypolicyviolation', (e) => resolve(e.violatedDirective), { once: true }));
    const script = document.createElement('script');
    script.textContent = 'window.injected = true;';
    document.body.appendChild(script);
    return { directive: await seen, injected: (window as unknown as { injected?: boolean }).injected === true };
  });
  expect(ran).toEqual({ directive: 'script-src-elem', injected: false });
  expect(errors).toHaveLength(1);
  expect(errors[0]).toMatch(/^Executing inline script violates the following Content Security Policy directive 'script-src 'self' https:\/\/challenges\.cloudflare\.com 'sha256-/);
});

test('the sign-in page and the function may not be framed, and the function is never cached', async ({ request }) => {
  for (const url of ['/signin', '/signup', '/api/health', '/api/auth/get-session', '/api/no/such/endpoint', '/no/such/page']) {
    const headers = (await request.get(url, { maxRedirects: 0 })).headers();
    expect(headers['x-frame-options'], url).toBe('DENY');
    expect(headers['content-security-policy'], url).toContain("frame-ancestors 'none'");
    expect(headers['x-content-type-options'], url).toBe('nosniff');
    expect(headers['referrer-policy'], url).toBe('strict-origin-when-cross-origin');
    if (url.startsWith('/api')) expect(headers['cache-control'], url).toBe('no-store');
  }
});
