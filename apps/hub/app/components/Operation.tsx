// A single API reference page: method + path, summary, description, the request body's fields (and its full JSON
// schema), each response, and a ready-to-run curl example.
import type { Operation as OperationData } from '../lib/openapi.server';
import { CodeBlock } from './docs/CodeBlock';
import { Markdown } from './docs/Markdown';
import { PAGE_TITLE, SECTION_HEADING, TABLE_FRAME, TD, TH } from './styles';
import { Tag, type TagTone } from './Tag';

const METHOD_TONE: Record<string, TagTone> = { GET: 'ink', POST: 'accent', PUT: 'accent' };

interface SchemaObject {
  type?: string | string[];
  properties?: Record<string, SchemaObject>;
  required?: string[];
  description?: string;
  items?: SchemaObject;
}

function isSchemaObject(value: unknown): value is SchemaObject {
  return typeof value === 'object' && value !== null;
}

interface FieldRow { name: string; type: string; required: boolean; description: string; depth: number }

function typeOf(schema: SchemaObject): string {
  if (Array.isArray(schema.type)) return schema.type.join(' | ');
  return schema.type ?? 'any';
}

// Flattens a schema's `properties` into rows, each nested object's own properties following it indented one level
// deeper -- so a field table reads top to bottom without a tree widget.
function fieldsOf(schema: unknown, depth = 0): FieldRow[] {
  if (!isSchemaObject(schema) || schema.properties === undefined) return [];
  const required = new Set(schema.required ?? []);
  const rows: FieldRow[] = [];
  for (const [name, prop] of Object.entries(schema.properties)) {
    if (!isSchemaObject(prop)) continue;
    rows.push({ name, type: typeOf(prop), required: required.has(name), description: prop.description ?? '', depth });
    if (prop.properties !== undefined) rows.push(...fieldsOf(prop, depth + 1));
    if (isSchemaObject(prop.items) && prop.items.properties !== undefined) rows.push(...fieldsOf(prop.items, depth + 1));
  }
  return rows;
}

function FieldTable({ schema }: { schema: unknown }) {
  const rows = fieldsOf(schema);
  if (rows.length === 0) return null;
  return (
    <div className={`mt-4 ${TABLE_FRAME}`}>
      <table className="w-full text-left text-sm">
        <thead>
          <tr>
            <th className={TH}>Field</th>
            <th className={TH}>Type</th>
            <th className={TH}>Required</th>
            <th className={TH}>Description</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={`${row.name}-${i}`}>
              <td className={`${TD} font-mono text-xs text-text`} style={{ paddingLeft: `${1 + row.depth}rem` }}>
                {row.name}
              </td>
              <td className={`${TD} font-mono text-xs text-soft`}>{row.type}</td>
              <td className={`${TD} text-xs text-soft`}>{row.required ? 'Yes' : 'No'}</td>
              <td className={`${TD} text-sm text-soft`}>{row.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SchemaDetails({ schema, label }: { schema: unknown; label: string }) {
  if (schema === null) return null;
  return (
    <details className="mt-4">
      <summary className="cursor-pointer font-mono text-xs text-mute hover:text-text">{label}</summary>
      <div className={`mt-2 p-4 ${TABLE_FRAME}`}>
        <pre className="font-mono text-xs leading-5 text-text">{JSON.stringify(schema, null, 2)}</pre>
      </div>
    </details>
  );
}

export function Operation({ operation }: { operation: OperationData }) {
  return (
    <div>
      <div className="flex items-center gap-2">
        <Tag tone={METHOD_TONE[operation.method] ?? 'ink'}>{operation.method}</Tag>
        <code className="font-mono text-sm text-mute">{operation.path}</code>
      </div>
      <h1 className={`mt-3 ${PAGE_TITLE}`}>{operation.summary}</h1>
      {operation.description !== '' && <Markdown body={operation.description} />}

      {operation.requestSchema !== null && (
        <section className="mt-10">
          <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Request body</h2>
          <FieldTable schema={operation.requestSchema} />
          <SchemaDetails schema={operation.requestSchema} label="Full request schema" />
        </section>
      )}

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Responses</h2>
        {operation.responses.map((response) => (
          <div key={response.status} className="mt-4 border border-ink bg-panel p-4">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-semibold text-text">{response.status}</span>
              <span className="text-sm text-soft">{response.description}</span>
            </div>
            {response.schema !== null && <SchemaDetails schema={response.schema} label="Response schema" />}
          </div>
        ))}
      </section>

      <section className="mt-10">
        <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>Example</h2>
        <CodeBlock code={operation.curl} language="bash" />
      </section>
    </div>
  );
}
