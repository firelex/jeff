// The terms of service and the privacy policy (content/legal/*.md, shown at /terms and /privacy) are drafts until the
// owner and a lawyer approve them. Two things mark a draft: the banner a page with `draft: true` shows, and each
// question only the owner can answer, written in the text as [OWNER: ...]. scripts/check-build.mjs refuses a
// production build (VERCEL_ENV=production) while any built file still holds either, so a draft cannot ship by accident.
// Plain TypeScript with no imports, so check-build.mjs can load it as it loads private-markers.ts.
export const DRAFT_BANNER = 'Draft, not yet approved';
export const OWNER_MARKER = '[OWNER:';

// Why `text` may not ship in a production build: one line per mark it holds, none when it holds neither.
export function draftMarksIn(text: string): string[] {
  const marks: string[] = [];
  if (text.includes(DRAFT_BANNER)) marks.push(`the draft banner "${DRAFT_BANNER}"`);
  const questions = text.split(OWNER_MARKER).length - 1;
  if (questions > 0) marks.push(`${questions} unanswered ${OWNER_MARKER} ...] question${questions === 1 ? '' : 's'}`);
  return marks;
}
