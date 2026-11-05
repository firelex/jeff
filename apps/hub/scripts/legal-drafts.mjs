// The check check-build.mjs runs on a production build (VERCEL_ENV=production, the setting Vercel gives a production
// deployment; app/lib/env.server.ts reads it the same way): no built file may still hold the draft banner of the
// terms or the privacy policy, or an unanswered [OWNER: ...] question (app/lib/legal-draft.ts). Any other build (a
// preview, the browser checks, a local build) shows the drafts and passes.
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HUB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { draftMarksIn } = await import(path.join(HUB_ROOT, 'app/lib/legal-draft.ts'));

// `files`: each built file as { where, text }. `env`: the build's environment (process.env). One line per file that
// may not ship; none when the build is not a production one.
export function draftProblems(files, env) {
  if (env.VERCEL_ENV !== 'production') return [];
  return files.flatMap(({ where, text }) => draftMarksIn(text).map((mark) => `${where}: still holds ${mark}`));
}

export const DRAFT_HELP = 'The terms of service and the privacy policy (content/legal/) are drafts: answer every [OWNER: ...] question '
  + 'and set draft: false in their front matter once the owner and a lawyer have approved them.';
