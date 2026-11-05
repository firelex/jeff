// The hidden adapters (content/owner-only/catalogue-text.yaml: every adapter the API serves that is not open; owner,
// 4 November 2026: "They are private"), as the owner's page /admin/adapters shows them and GET /api/admin/adapters
// returns them: each one's catalogue entry, how it was measured (content/owner-only/adapter-numbers.yaml) and its
// worked examples (content/owner-only/catalogue-examples.yaml and examples/), each with the sentence on what it shows
// and the POST /v1/decide body that makes its call. scripts/function-content.mjs writes them into
// owner-only/hidden-adapters.generated.ts (HIDDEN_ADAPTERS), so no public file carries them.
import type { AdapterNumbers } from './adapter-numbers.server.js';
import type { CatalogueEntry } from './catalogue.server.js';
import type { Example } from './examples.server.js';

export interface HiddenExample { shows: string; example: Example; request: { calls: Record<string, unknown>[] } }
// numbers is null only for an adapter that content/owner-only/without-numbers.yaml names.
export interface HiddenAdapter { entry: CatalogueEntry; numbers: AdapterNumbers | null; examples: HiddenExample[] }
