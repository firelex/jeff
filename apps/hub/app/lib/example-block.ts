// What a page shows for one worked example, shared by the docs loader (which writes it into the page as a fenced
// block of JSON in EXAMPLE_LANGUAGE), the catalogue's adapter pages, and the browser (components/docs/Example.tsx
// renders it). No runtime code from a .server file, so the browser bundle can import it.
import type { SdkLanguage } from './sdk-languages.js';

export const EXAMPLE_LANGUAGE = 'jeff-example';

// One named value of the case, as text: a context field (its code name) or one of the caller's labels (its key).
export interface ExampleField { name: string; value: string }

export interface ExampleBlock {
  id: string;
  // The context fields the call passes, in the example's order (empty when it passes none).
  context: ExampleField[];
  input: string;
  // The labels the caller passes, each with its description ('' when it has none); null when the function's own
  // labels are used.
  labels: ExampleField[] | null;
  // The call in each SDK language the site shows (content/sdk-languages.yaml), in tab order; only Python for an
  // offline example, which runs with the Python SDK on the reader's machine.
  code: { language: SdkLanguage; code: string }[];
  // null for an offline example: its adapter is not served by the API, so there is no request to show.
  curl: string | null;
  // The same call made on the reader's machine ("On your machine"), in the same languages as `code`, and the id of the
  // adapter the local engine serves for it; null when there is none to show (a proprietary adapter, a via-http
  // example, or an offline one, whose code is already local): the block then offers no choice.
  local: { adapter: string; code: { language: SdkLanguage; code: string }[] } | null;
  // The answer recorded from the live API, with its options of highest probability; null for an offline example.
  answer: { value: string; confidence: string; top: { option: string; probability: string }[] } | null;
  // Its "Open in playground" (playground-samples.server.ts: playgroundLink): the playground with this example chosen
  // when it is a sample, else with its adapter's first sample; null when the playground has none of the adapter's.
  playground: string | null;
}
