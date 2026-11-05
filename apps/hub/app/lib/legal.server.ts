// The terms of service and the privacy policy: content/legal/<name>.md, read when the site is built (the pages are
// prerendered; the function never reads these files). Front matter gives the title and whether the page is still a
// draft (app/lib/legal-draft.ts: its banner); {{price}} and {{allowance}} in the body become the plan's settings, as on /pricing.
import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import { DRAFT_BANNER, OWNER_MARKER } from './legal-draft.js';
import { HUB_ROOT } from './paths.server.js';
import { priceText, publicSettings, tokensText } from './public-settings.server.js';

export type LegalName = 'terms' | 'privacy';
// `banner`: the draft banner's text while the page is a draft, else null.
export interface LegalPage { title: string; banner: string | null; body: string }

export function parseLegalPage(where: string, text: string, plan: { price: string; allowance: string }): LegalPage {
  const match = /^---\n([\s\S]*?)\n---\n/.exec(text);
  if (match === null) throw new Error(`${where}: starts without front matter (---, title: ..., draft: ..., ---)`);
  const front = parse(match[1]) as { title?: unknown; draft?: unknown };
  if (typeof front.title !== 'string' || front.title === '') throw new Error(`${where}: front matter has no title`);
  if (typeof front.draft !== 'boolean') throw new Error(`${where}: front matter's draft must be true or false`);
  const body = text.slice(match[0].length).replaceAll('{{price}}', plan.price).replaceAll('{{allowance}}', plan.allowance);
  if (!front.draft && body.includes(OWNER_MARKER)) throw new Error(`${where}: says draft: false but still has ${OWNER_MARKER} ...] questions`);
  const unknown = body.match(/\{\{[^}]*\}\}/g);
  if (unknown !== null) throw new Error(`${where}: has placeholders it may not use: ${[...new Set(unknown)].join(', ')}`);
  return { title: front.title, banner: front.draft ? `${DRAFT_BANNER}. This text is being reviewed and is not in force yet.` : null, body };
}

export function legalPage(name: LegalName): LegalPage {
  const file = path.join(HUB_ROOT, 'content/legal', `${name}.md`);
  const shown = publicSettings();
  return parseLegalPage(`content/legal/${name}.md`, fs.readFileSync(file, 'utf8'),
    { price: priceText(shown.priceUsd), allowance: tokensText(shown.allowanceInputTokens) });
}
