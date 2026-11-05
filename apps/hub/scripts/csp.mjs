// The pages' script policy. vercel.json gives every page its Content-Security-Policy header (frame-ancestors 'none',
// what may load from where); a static header cannot name the inline scripts React Router writes into each
// prerendered page (its context, the streamed loader data), which differ per page. So each built page also carries
// a <meta http-equiv="Content-Security-Policy"> with only a script-src: the hub itself, Cloudflare Turnstile's script,
// and the SHA-256 of every inline script that page has. A browser enforces both policies, so a script runs only when
// it comes from those origins or is one of the page's own inline scripts, byte for byte. Used by
// scripts/csp-meta.mjs (writes the meta tags after the build) and scripts/check-build.mjs (checks them). The header's
// own script-src allows 'unsafe-inline' for this reason only: with it, the meta tag's hashes decide which inline
// scripts run (a header cannot list them); without it, the header would refuse every inline script.
import crypto from 'node:crypto';

export const SCRIPT_ORIGINS = ["'self'", 'https://challenges.cloudflare.com'];

const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script>/g;
const META = /<meta http-equiv="Content-Security-Policy" content="([^"]*)"\/>/;

// The 'sha256-...' source of every inline script (one without a src) in the page, in page order.
export function inlineScriptHashes(html) {
  const hashes = [];
  for (const [, attributes, body] of html.matchAll(SCRIPT)) {
    if (/\bsrc=/.test(attributes)) continue;
    hashes.push(`'sha256-${crypto.createHash('sha256').update(body, 'utf8').digest('base64')}'`);
  }
  return hashes;
}

export function scriptPolicy(html) {
  return `script-src ${[...SCRIPT_ORIGINS, ...new Set(inlineScriptHashes(html))].join(' ')}`;
}

// React Router's pages start so; the policy goes right after the charset, before any script. Its redirect pages
// (/docs) are a short page of their own with no script; there it goes right after <head>.
const HEAD = '<!DOCTYPE html><html lang="en"><head><meta charSet="utf-8"/>';

// The page with its script policy first in <head>.
export function withScriptPolicy(html) {
  if (META.test(html)) throw new Error('the page already has a Content-Security-Policy meta tag');
  const meta = `<meta http-equiv="Content-Security-Policy" content="${scriptPolicy(html)}"/>`;
  if (html.startsWith(HEAD)) return `${HEAD}${meta}${html.slice(HEAD.length)}`;
  if (!/<script\b/.test(html) && html.includes('<head>')) return html.replace('<head>', `<head>${meta}`);
  throw new Error(`the page neither starts with ${HEAD} nor is a page without scripts`);
}

// What is wrong with a built page's script policy, or null when it is the one its scripts need.
export function scriptPolicyProblem(html) {
  const found = META.exec(html);
  if (found === null) return 'has no Content-Security-Policy meta tag';
  const firstScript = html.search(/<script\b/);
  if (firstScript !== -1 && firstScript < found.index) return 'has a script before its script policy';
  const expected = scriptPolicy(html);
  if (found[1] !== expected) return `has the script policy "${found[1]}", but its inline scripts need "${expected}"`;
  return null;
}
