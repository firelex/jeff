import fs from 'node:fs';
import path from 'node:path';
import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { describe, expect, it } from 'vitest';
import type { CatalogueEntry } from '../app/lib/catalogue.server';
import { codeNameOf } from '../app/lib/catalogue.server';
import { filterEntries } from '../app/components/catalogue/Filters';
import { adapterCall, placeholderExample } from '../app/lib/catalogue-call.server';
import { catalogue, shownLanguages } from '../app/lib/site.server';

const entry = (id: string, kind: 'open' | 'proprietary', iface: string) => ({
  id, version: '1.4.0', kind, domain: id.split('.')[1], title: id, summary: 's', useWhen: [], avoidWhen: [],
  base: 'jeff-base v1.4', functions: [{ key: 'f', id: `${id}.f`, interface: iface, instructions: null, labels: null, context: null, input: null }],
  context: [{ key: 'application', codeName: 'application', description: 'd', required: true }],
  input: { key: 'text', codeName: 'text', description: 'd' },
  weights: kind === 'open' ? { repo: 'mstrasser/jeff-adapter-x', revision: 'v1.4' } : null,
  licence: kind === 'open' ? 'Apache-2.0' : null,
  localName: kind === 'open' ? id.slice(id.lastIndexOf('.') + 1) : null,
});

// A context field whose key has an apostrophe and spaces, the only kind of key that exposed the bug the review found
// (a real API-only adapter has such keys): the manifest declares no code_name for it, so it is derived, and the
// derivation (counterparty_name-style) strips the apostrophe -- while the raw key keeps it, which is also what the
// snippet's shell-escaping test needs.
const AFFECTED_KEY = "reviewer's note on the draft";
const AFFECTED_CODE_NAME = codeNameOf(AFFECTED_KEY);

const affectedEntry = (id: string, kind: 'open' | 'proprietary') => ({
  ...entry(id, kind, 'classify'),
  context: [{ key: AFFECTED_KEY, codeName: AFFECTED_CODE_NAME, description: 'd', required: true }],
});

// The exact reverse of lib/shell.ts's singleQuoted: strips the wrapping quotes and turns each '\'' back into '.
// Decoding (rather than just regex-capturing the inside) is what actually proves the curl argument is one valid
// POSIX shell word, whatever characters the JSON body contains.
function decodeSingleQuoted(word: string): string {
  if (!word.startsWith("'") || !word.endsWith("'")) throw new Error(`not a single-quoted shell word: ${word}`);
  return word.slice(1, -1).split(String.raw`'\''`).join("'");
}

function bodyOf(curl: string): unknown {
  const match = /-d (.*)$/s.exec(curl);
  if (match === null) throw new Error(`curl snippet has no -d argument: ${curl}`);
  return JSON.parse(decodeSingleQuoted(match[1]));
}

describe('catalogue', () => {
  const all = [entry('jeff.safety.guard', 'open', 'detect'), entry('jeff.operations.sample', 'proprietary', 'classify')] as unknown as CatalogueEntry[];
  it('filters by interface, domain and words (not by kind: owner, 4 November 2026)', () => {
    expect(filterEntries(all, { query: '', interface: null, domain: 'safety' }).map((e) => e.id)).toEqual(['jeff.safety.guard']);
    expect(filterEntries(all, { query: '', interface: 'classify', domain: null }).map((e) => e.id)).toEqual(['jeff.operations.sample']);
    expect(filterEntries(all, { query: 'sample', interface: null, domain: null })).toHaveLength(1);
    expect(filterEntries(all, { query: '', interface: null, domain: null })).toHaveLength(2);
  });
  const languages = shownLanguages();
  it('gives every adapter page its call in every shown language and curl: the SDK for an open adapter, plain HTTP for a proprietary one', () => {
    const open = adapterCall(all[0], 'https://api.example.com', languages);
    expect(open.code.map((c) => c.language)).toEqual(languages);
    expect(open.code[0].code).toContain('jeff.Jeff.remote("https://api.example.com"');
    expect(open.code[0].code).toContain('"jeff.safety.guard.f"');
    const proprietary = adapterCall(all[1], 'https://api.example.com', languages);
    expect(proprietary.code.map((c) => c.language)).toEqual(languages);
    expect(proprietary.code[0].code).toContain('httpx.post(');
    expect(proprietary.curl).toContain('https://api.example.com/v1/decide');
  });
  it("gives an open adapter's call its local form with the adapter's short name, and a proprietary one none", () => {
    const open = adapterCall(all[0], 'https://api.example.com', languages);
    expect(open.local?.adapter).toBe('jeff.safety.guard');
    expect(open.local?.code.map((c) => c.language)).toEqual(languages);
    expect(open.local?.code[0].code).toContain('client = jeff.Jeff.local(["guard"])');
    expect(adapterCall(all[1], 'https://api.example.com', languages).local).toBeNull();
  });
  it("names every real open adapter locally by a short name no other adapter shares", () => {
    const entries = catalogue();
    const open = entries.filter((e) => e.kind === 'open');
    expect(open.length).toBeGreaterThan(0);
    const names = open.map((e) => e.localName);
    expect(names.every((n) => typeof n === 'string' && n !== '')).toBe(true);
    expect(new Set(names).size).toBe(names.length);
    expect(entries.filter((e) => e.kind === 'proprietary').every((e) => e.localName === null)).toBe(true);
  });
  it('calls the first function with "<input key>" and each context field as "<code name>", keyed by its code name', () => {
    expect(placeholderExample(all[0])).toMatchObject({ function: 'jeff.safety.guard.f', input: '<text>',
      context: { application: '<application>' }, via: 'sdk', offline: false });
    expect(placeholderExample(all[1]).via).toBe('http');
  });
  it("every real adapter's call names its function and placeholders in every language, and its curl body validates against contract/schemas/decide_request.schema.json", () => {
    const schemaDir = path.join(__dirname, '../../../contract/schemas');
    const ajv = new Ajv2020({ allErrors: true, strict: false });
    addFormats(ajv);
    ajv.addSchema(JSON.parse(fs.readFileSync(path.join(schemaDir, 'graph.schema.json'), 'utf8')));
    const validate = ajv.compile(JSON.parse(fs.readFileSync(path.join(schemaDir, 'decide_request.schema.json'), 'utf8')));
    for (const e of catalogue()) {
      const call = adapterCall(e, 'https://api.example.com', languages);
      const placeholder = placeholderExample(e);
      for (const { language, code } of call.code) {
        expect(code, `${e.id} ${language}`).toContain(placeholder.function);
        for (const value of [placeholder.input as string, ...Object.values(placeholder.context ?? {}) as string[]]) {
          // Each language writes the placeholder as a string literal; its text (no quote or backslash in a placeholder
          // but an apostrophe, which none of them escapes) appears as it is.
          expect(code, `${e.id} ${language}`).toContain(value);
        }
      }
      const body = bodyOf(call.curl);
      expect(validate(body), `${e.id}: ${ajv.errorsText(validate.errors)}`).toBe(true);
    }
  });

  describe('a field whose key is not a code name', () => {
    const open = affectedEntry('jeff.operations.sample', 'open') as unknown as CatalogueEntry;
    const proprietary = affectedEntry('jeff.operations.sample', 'proprietary') as unknown as CatalogueEntry;

    it("keys the curl and SDK context by the field's code name, never its raw key", () => {
      const body = bodyOf(adapterCall(proprietary, 'https://api.example.com', languages).curl) as { calls: [{ context?: Record<string, unknown> }] };
      expect(body.calls[0].context).toEqual({ [AFFECTED_CODE_NAME]: `<${AFFECTED_CODE_NAME}>` });
      const python = adapterCall(open, 'https://api.example.com', languages).code[0].code;
      expect(python).toContain(`"${AFFECTED_CODE_NAME}"`);
      expect(python).not.toContain(`"${AFFECTED_KEY}"`);
    });

    it('produces a curl body with a valid shell string when the input key carries an apostrophe', () => {
      const withApostrophe = { ...proprietary, input: { key: AFFECTED_KEY, codeName: AFFECTED_CODE_NAME, description: 'd' } } as CatalogueEntry;
      const { curl } = adapterCall(withApostrophe, 'https://api.example.com', languages);
      const line = curl.split('\n').at(-1) ?? '';
      expect(line.startsWith('  -d ')).toBe(true);
      const decoded = decodeSingleQuoted(line.slice('  -d '.length));
      // Decoding recovers exactly the JSON text that was wrapped -- proof the argument is one shell word, not two
      // broken apart by the embedded apostrophe.
      expect(decoded).toBe(JSON.stringify(JSON.parse(decoded)));
      expect(JSON.parse(decoded)).toMatchObject({ calls: [{ input: `<${AFFECTED_KEY}>` }] });
    });
  });
});
