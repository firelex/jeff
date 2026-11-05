import { expect, test } from './fixtures';

// One page of each kind, opened in the static build as it ships (playwright.config.ts): each must load with no
// console error and no uncaught error (e2e/fixtures.ts), and show its own heading once hydrated. Then what the host
// does with addresses the build has no page for, as vercel.json sets it up.

const PAGES: [string, string][] = [
  ['/', 'Millisecond Decisions, Any Domain'],
  ['/docs/guides/introduction', 'Introduction'],
  ['/docs/guides/quickstart', 'Quick start'],
  ['/docs/sdk/install', 'Install'],
  ['/docs/sdk/node', 'Node SDK'],
  ['/docs/sdk/rust', 'Rust SDK'],
  ['/docs/sdk/csharp', 'C# SDK'],
  ['/docs/sdk/java', 'Java SDK'],
  ['/docs/api', 'API reference'],
  ['/docs/api/post-v1-decide', 'Run the planner'],
  ['/catalogue', 'Catalogue'],
  ['/catalogue?domain=safety', 'Catalogue'],
  ['/catalogue/jeff.safety.guard', 'Prompt injection guard'],
  ['/catalogue/jeff.compliance.sanctions', 'Sanctions and watchlist screening'],
  ['/terms', 'Terms of service'],
  ['/privacy', 'Privacy policy'],
];

// The terms and the privacy policy (Google's sign-in consent screen links to both): reached from the footer and the
// sign-in page, marked as drafts while they are, and no wider than a phone.
for (const [url, heading, name] of [['/terms', 'Terms of service', 'Terms'], ['/privacy', 'Privacy policy', 'Privacy']] as const) {
  test(`${url} is linked from the footer and the sign-in page, says it is a draft and fits at 375 px`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto('/');
    await page.getByRole('contentinfo').getByRole('link', { name, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${url}$`));
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(heading);
    await expect(page.getByRole('note')).toContainText('Draft, not yet approved');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0);
    await page.goto('/signin');
    await expect(page.getByRole('main').getByRole('link', { name: url === '/terms' ? 'terms of service' : 'privacy policy' })).toHaveAttribute('href', url);
  });
}

// Every page has the footer (root.tsx), with its links, and it fits at 375 px: the pages above, and the pages that
// load their content in the browser (pricing, sign-in, the account, the playground, the request form and the owner's
// pages, whose footer is there whether or not anyone is signed in); the Not found pages are checked below.
const FOOTER_LINKS: [string, string][] = [
  ['GitHub', 'https://github.com/firelex/jeff'], ['Docs', '/docs/guides/introduction'], ['Catalogue', '/catalogue'],
  ['Terms', '/terms'], ['Privacy', '/privacy'],
];
const FOOTER_PAGES = [...PAGES.map(([url]) => url), '/pricing', '/signin', '/account', '/playground', '/request-adapter', '/admin/requests', '/admin/adapters'];
test('every page has the footer with its links, and it fits at 375 px', async ({ page, allowFailedLoad }) => {
  // Signed out, the owner's pages and the request form ask the hub's function and are answered 401 or 404.
  // Signed out, /account and the request form go on to /signin (a 401 first), and the owner's pages say Not found
  // (a 404 to anyone but the owner).
  for (const path of ['/api/account', '/api/account/status', '/api/adapter-requests', '/api/admin/adapter-requests', '/api/admin/adapters']) allowFailedLoad(path);
  // The bot check's widget (the playground, sign-in), answered here so no outside service is reached.
  await page.route('https://challenges.cloudflare.com/**', (route) => route.fulfill({ contentType: 'text/javascript',
    body: 'window.turnstile = { render: () => "w", remove: () => {} };' }));
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 375, height: 812 });
  for (const url of FOOTER_PAGES) {
    await page.goto(url);
    const footer = page.getByRole('contentinfo');
    await expect(footer, url).toBeVisible();
    await expect(footer.getByRole('link'), url).toHaveText(FOOTER_LINKS.map(([name]) => name));
    for (const [name, href] of FOOTER_LINKS) await expect(footer.getByRole('link', { name, exact: true }), url).toHaveAttribute('href', href);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth), url).toBe(0);
  }
});

for (const [url, heading] of PAGES) {
  test(`${url} loads and hydrates with no errors`, async ({ page }) => {
    const response = await page.goto(url);
    expect(response?.status()).toBe(200);
    await expect(page.getByRole('heading', { level: 1 }).first()).toContainText(heading);
    // Hydrated: a client-side link works without a full page load.
    await page.waitForLoadState('networkidle');
  });
}

test('a client-side navigation to another page works with no errors', async ({ page }) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.getByRole('link', { name: 'Catalogue' }).first().click();
  await expect(page).toHaveURL(/\/catalogue$/);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Catalogue');
});

test('an address with no page answers 404 with the Not found page, in the site layout', async ({ page, allowFailedLoad }) => {
  allowFailedLoad('/no/such/page');
  const response = await page.goto('/no/such/page');
  expect(response?.status()).toBe(404);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Not found');
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Not found');
  await expect(page.getByRole('link', { name: 'Docs' }).first()).toBeVisible();
  await expect(page.getByRole('contentinfo').getByRole('link', { name: 'Privacy' })).toBeVisible();
});

test('a docs address with no page answers 404 with the Not found page, which runs no script', async ({ page, allowFailedLoad }) => {
  allowFailedLoad('/docs/guides/nope');
  const response = await page.goto('/docs/guides/nope');
  expect(response?.status()).toBe(404);
  await page.waitForLoadState('networkidle');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Not found');
  expect(await page.locator('script').count()).toBe(0);
  await expect(page.getByRole('contentinfo').getByRole('link', { name: 'Terms' })).toHaveAttribute('href', '/terms');
});

test('a link inside the site to an adapter that does not exist shows the Not found page', async ({ page, allowFailedLoad }) => {
  allowFailedLoad('/catalogue/jeff.no-such.adapter.data');
  await page.goto('/catalogue');
  await page.waitForLoadState('networkidle');
  // A client-side navigation, as a stale link would make (react-router's framework mode exposes its router here).
  await page.evaluate(async () => {
    const router = (window as unknown as { __reactRouterDataRouter: { navigate: (to: string) => Promise<void> } }).__reactRouterDataRouter;
    await router.navigate('/catalogue/jeff.no-such.adapter');
  });
  await expect(page).toHaveURL(/\/catalogue\/jeff\.no-such\.adapter$/);
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Not found');
});

test('/docs redirects permanently to the introduction', async ({ page, request }) => {
  const direct = await request.get('/docs', { maxRedirects: 0 });
  expect(direct.status()).toBe(308);
  expect(direct.headers().location).toBe('/docs/guides/introduction');
  await page.goto('/docs');
  await expect(page).toHaveURL(/\/docs\/guides\/introduction$/);
});

test('an address with a trailing slash redirects to the one without it', async ({ request }) => {
  const response = await request.get('/catalogue/', { maxRedirects: 0 });
  expect(response.status()).toBe(308);
  expect(response.headers().location).toBe('/catalogue');
});
