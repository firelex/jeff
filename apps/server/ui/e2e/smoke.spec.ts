// A browser smoke test of every console page, against the committed build (apps/server/src/jeff_server/ui) served
// by a real Jeff server over a fake engine (apps/server/tests/console_server.py, started by ./global-setup.ts). One
// screenshot per page is saved under results/console/ (repository root), committed for the owner to review.
import { test, expect, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCREENSHOT_DIR = path.resolve(HERE, '../../../../results/console');
fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set; run this suite through "pnpm --filter jeff-console e2e" (its ` +
      'playwright.config.ts global setup starts the test server and sets it)');
  }
  return value;
}

const serverUrl = (): string => requiredEnv('JEFF_CONSOLE_E2E_URL');
const usersFilePath = (): string => requiredEnv('JEFF_CONSOLE_E2E_USERS_FILE');
const adminAccount = (): { email: string; password: string } =>
  ({ email: requiredEnv('JEFF_CONSOLE_E2E_ADMIN_EMAIL'), password: requiredEnv('JEFF_CONSOLE_E2E_ADMIN_PASSWORD') });
const memberAccount = (): { email: string; password: string } =>
  ({ email: requiredEnv('JEFF_CONSOLE_E2E_MEMBER_EMAIL'), password: requiredEnv('JEFF_CONSOLE_E2E_MEMBER_PASSWORD') });

function screenshot(page: Page, name: string): Promise<Buffer> {
  return page.screenshot({ path: path.join(SCREENSHOT_DIR, `${name}.png`) });
}

function rail(page: Page) {
  return page.getByRole('navigation', { name: 'Console' });
}

async function signIn(page: Page, email: string, password: string): Promise<void> {
  await page.goto(`${serverUrl()}/ui/`);
  await page.getByLabel('Login').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Jeff Console' })).toBeVisible();
}

test('sign in, visit every page, run a function, switch an adapter, play a factory replay, open a doc page, see the access log as an admin', async ({ page }) => {
  const { email, password } = adminAccount();
  // The page is served with a Content-Security-Policy (jeff_server.console.CONSOLE_CSP); anything it blocks --
  // a script, a style, a font, Mermaid's diagram -- is reported by Chromium on the console and fails the test.
  const blocked: string[] = [];
  page.on('console', (message) => {
    if (/Content Security Policy/i.test(message.text())) blocked.push(message.text());
  });

  await page.goto(`${serverUrl()}/ui/`);
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
  await screenshot(page, 'sign-in');

  await signIn(page, email, password);
  await expect(page.getByText('Try it with an example').first()).toBeVisible();
  await screenshot(page, 'home');

  // Run one function in the playground, seeded from the home page's first example, and see a Decision. The card's
  // button has its own aria-label ("Try <function id>"), so it is found by its visible text instead.
  await page.getByText('Try it with an example').first().click();
  await expect(page.getByRole('heading', { level: 1, name: 'Run a function' })).toBeVisible();
  await page.getByRole('button', { name: 'Run' }).click();
  await expect(page.getByTestId('decision-value').first()).toBeVisible();
  await screenshot(page, 'playground');

  // Switch an adapter off, then back on.
  await rail(page).getByRole('button', { name: 'Adapters' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Adapters' })).toBeVisible();
  const toggle = page.getByRole('switch').first();
  await expect(toggle).toHaveAttribute('aria-checked', 'true');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'true');
  await screenshot(page, 'adapters');

  // The factory page, on a server with building off and one recorded run: the off message, the replay played to its
  // end, and the published adapter's function.
  await rail(page).getByRole('button', { name: 'Factory' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Make an adapter' })).toBeVisible();
  await expect(page.getByText('Building adapters is switched off on this server (JEFF_FACTORY=off)')).toBeVisible();
  const position = page.getByRole('slider', { name: 'Position in the run' });
  await expect(position).toBeVisible();
  await position.fill((await position.getAttribute('max')) as string);
  const published = page.getByRole('region', { name: 'Publish' });
  await expect(published.getByText('jeff.factory.parcel-claims', { exact: true })).toBeVisible();
  await expect(page.getByRole('list', { name: 'Train steps' }).getByLabel('done')).toHaveCount(2);
  await screenshot(page, 'factory');
  await published.scrollIntoViewIfNeeded();
  await screenshot(page, 'factory-published');

  // An admin sees the access log.
  await rail(page).getByRole('button', { name: 'Access log' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Access log' })).toBeVisible();
  await expect(page.getByText(/console visits from/)).toBeVisible();
  await screenshot(page, 'access-log');

  // The API reference: its own heading and a route card, so the screenshot is never of the page before it. A path
  // in its list at the top scrolls to that route's card without changing the console's route.
  await rail(page).getByRole('button', { name: 'API reference' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'API reference' })).toBeVisible();
  const decideCard = page.locator('#route-post-_v1_decide');
  await expect(decideCard).toBeVisible();
  await screenshot(page, 'api-reference');
  await page.getByRole('navigation', { name: 'Every path' }).getByRole('button', { name: /post \/v1\/decide$/i }).click();
  await expect(decideCard).toBeInViewport();
  expect(new URL(page.url()).hash).toBe('#api');
  await expect(page.getByRole('heading', { level: 1, name: 'API reference' })).toBeVisible();

  // A documentation page.
  await rail(page).getByRole('button', { name: 'Documentation' }).click();
  await page.getByRole('navigation', { name: 'Documentation' }).getByRole('link', { name: 'Introduction' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Introduction' })).toBeVisible();
  await expect(page.locator('[data-slot="mermaid-block"] svg').first()).toBeVisible();
  await screenshot(page, 'docs');
  expect(blocked).toEqual([]);
});

test('a non-admin does not see the access log', async ({ page }) => {
  const { email, password } = memberAccount();
  await signIn(page, email, password);
  await expect(rail(page).getByRole('button', { name: 'Access log' })).toHaveCount(0);
});

test("removing the signed-in user from the users file mid-session returns to sign-in with the server's message", async ({ page }) => {
  const { email, password } = memberAccount();
  await signIn(page, email, password);
  // Let the home page's own initial calls (GET /v1/functions, /ui/examples, /health, POST /ui/events) finish while
  // the login is still good. Otherwise one of them can still be in flight when the login is removed below, 401 on
  // the page that is about to be replaced, and sign it out there -- clearing the stored session before the reload
  // below ever runs its own check, so the reload would find no session at all instead of a rejected one.
  await page.waitForLoadState('networkidle');

  const file = usersFilePath();
  const content = JSON.parse(fs.readFileSync(file, 'utf-8')) as { secret: string; users: Record<string, string> };
  delete content.users[email];
  fs.writeFileSync(file, JSON.stringify(content, null, 2));
  // Bump the file's mtime forward so the server's cache (keyed on it) is never in doubt about which write is newest.
  const future = new Date(Date.now() + 5000);
  fs.utimesSync(file, future, future);

  // The next call the console makes is refused (401): back to sign-in, with the server's own message, never a
  // blank or broken page. A reload (GET /ui/me, from the session already in localStorage) is the trigger here,
  // rather than a rail click: the click itself would race the console's own unmount once the 401 lands, since the
  // button being clicked lives inside the very nav that disappears.
  await page.reload();
  await expect(page.getByLabel('Login')).toBeVisible();
  await expect(page.getByText('Unauthorised', { exact: true })).toBeVisible();
  await expect(page.getByText('The credentials are not valid on this server', { exact: false })).toBeVisible();
});
