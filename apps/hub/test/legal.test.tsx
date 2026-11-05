// The terms of service and the privacy policy (content/legal/, /terms and /privacy): how a page is read, the draft
// banner, and the check that refuses a production build while a draft or an [OWNER: ...] question is left
// (scripts/legal-drafts.mjs, run by check-build.mjs).
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LegalPage } from '../app/components/LegalPage';
import { DRAFT_BANNER, draftMarksIn } from '../app/lib/legal-draft';
import { legalPage, parseLegalPage } from '../app/lib/legal.server';
// @ts-expect-error: a plain JavaScript script, with no type declarations.
import { draftProblems } from '../scripts/legal-drafts.mjs';

afterEach(() => vi.unstubAllEnvs());

const PLAN = { price: '$1', allowance: '50 million' };
const page = (front: string, body: string) => `---\n${front}\n---\n${body}`;

describe('a legal page', () => {
  it('reads its title, shows the banner while it is a draft and fills in the plan', () => {
    expect(parseLegalPage('t.md', page('title: Terms\ndraft: true', 'It costs {{price}} for {{allowance}} tokens.'), PLAN)).toEqual({
      title: 'Terms', banner: `${DRAFT_BANNER}. This text is being reviewed and is not in force yet.`, body: 'It costs $1 for 50 million tokens.',
    });
    expect(parseLegalPage('t.md', page('title: Terms\ndraft: false', 'Done.'), PLAN).banner).toBeNull();
  });

  it('refuses a page with no draft setting, an approved page with questions left, and an unknown placeholder', () => {
    expect(() => parseLegalPage('t.md', page('title: Terms', 'x'), PLAN)).toThrow("t.md: front matter's draft must be true or false");
    expect(() => parseLegalPage('t.md', page('title: Terms\ndraft: false', 'By [OWNER: name].'), PLAN))
      .toThrow('t.md: says draft: false but still has [OWNER: ...] questions');
    expect(() => parseLegalPage('t.md', page('title: Terms\ndraft: true', '{{origin}}'), PLAN)).toThrow('t.md: has placeholders it may not use: {{origin}}');
  });

  it('shows the banner above the title, and none once approved', () => {
    const { unmount } = render(<LegalPage title="Terms of service" banner="Draft, not yet approved. Not in force." body="Hello." />);
    expect(screen.getByRole('note').textContent).toBe('Draft, not yet approved. Not in force.');
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Terms of service');
    unmount();
    render(<LegalPage title="Terms of service" banner={null} body="Hello." />);
    expect(screen.queryByRole('note')).toBeNull();
  });

  it('reads both pages from content/legal with the plan settings', () => {
    vi.stubEnv('PLAN_PRICE_USD', '1');
    vi.stubEnv('PLAN_ALLOWANCE_INPUT_TOKENS', '50000000');
    vi.stubEnv('TURNSTILE_SITE_KEY', 'site');
    vi.stubEnv('PLAYGROUND_RUNS_PER_MINUTE', '5');
    vi.stubEnv('STRIPE_AUTOMATIC_TAX', 'off');
    vi.stubEnv('SIGN_IN_PROVIDERS', 'github,email');
    expect(legalPage('terms').title).toBe('Terms of service');
    expect(legalPage('terms').body).toContain('The API plan costs $1 a month and includes 50 million input tokens a month.');
    expect(legalPage('privacy').title).toBe('Privacy policy');
  });
});

describe('the production build check', () => {
  const files = [
    { where: 'build/client/terms/index.html', text: `<p>${DRAFT_BANNER}.</p><p>Run by [OWNER: name] at [OWNER: address].</p>` },
    { where: 'build/client/index.html', text: '<p>Home</p>' },
  ];

  it('names every draft banner and question left in a built file', () => {
    expect(draftMarksIn(files[0].text)).toEqual([`the draft banner "${DRAFT_BANNER}"`, '2 unanswered [OWNER: ...] questions']);
    expect(draftMarksIn('[OWNER: one]')).toEqual(['1 unanswered [OWNER: ...] question']);
    expect(draftMarksIn('<p>Home</p>')).toEqual([]);
  });

  it('refuses a production build that still holds a draft, and lets any other build through', () => {
    expect(draftProblems(files, { VERCEL_ENV: 'production' })).toEqual([
      `build/client/terms/index.html: still holds the draft banner "${DRAFT_BANNER}"`,
      'build/client/terms/index.html: still holds 2 unanswered [OWNER: ...] questions',
    ]);
    expect(draftProblems([files[1]], { VERCEL_ENV: 'production' })).toEqual([]);
    expect(draftProblems(files, { VERCEL_ENV: 'preview' })).toEqual([]);
    expect(draftProblems(files, {})).toEqual([]);
  });
});
