// Heading ids, shared by the loader (On this page, link checking) and the renderer, so they always agree. Same rule
// as the console's docs (apps/server/ui/src/docs/Docs.tsx), so moved links keep working.
export function headingId(text: string): string {
  return `section-${text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')}`;
}
