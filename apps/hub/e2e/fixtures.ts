import { expect, test as base } from '@playwright/test';

// Every check's page fails the check on any console error or uncaught page error. A check that expects one (the
// browser's own "Failed to load resource" line for a 404 it asked for, or for a refusal such as the sign-in form's
// 403) names its URL with allowFailedLoad first; any other error, or a failed load of any other URL, still fails.
export const test = base.extend<{ allowFailedLoad: (url: string) => void }>({
  allowFailedLoad: [async ({ page, baseURL }, use) => {
    const allowed = new Set<string>();
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(`page error: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() !== 'error') return;
      const failedLoad = /^Failed to load resource: the server responded with a status of \d{3}/.test(message.text());
      if (failedLoad && allowed.has(message.location().url)) return;
      errors.push(`console error at ${message.location().url}: ${message.text()}`);
    });
    await use((url) => allowed.add(new URL(url, baseURL).href));
    expect(errors, `errors on ${page.url()}`).toEqual([]);
  }, { auto: true }],
});

export { expect };
