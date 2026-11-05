// The adapter page's function list: key, interface, instructions, labels. A function that declares its own
// context or input (rather than taking the adapter's) shows them in a second row underneath -- most functions take
// the adapter's, so this only appears for the few that train on their own request shape.
import { Fragment } from 'react';
import type { CatalogueFunction } from '../../lib/catalogue.server';
import { TABLE_FRAME, TD, TH } from '../styles';

function labelsText(labels: string[] | null): string {
  return labels === null ? 'from the caller' : labels.join(', ');
}

// The code name is the name a caller passes; the raw, as-trained key appears alongside it only when they differ.
function fieldName(codeName: string, key: string): string {
  return codeName === key ? codeName : `${codeName} (as trained: ${key})`;
}

function contextText(context: CatalogueFunction['context']): string {
  if (context === null) return '';
  return context.length === 0
    ? 'none'
    : context.map((c) => `${fieldName(c.codeName, c.key)}${c.required ? '' : ' (optional)'}`).join(', ');
}

export function FunctionTable({ functions }: { functions: CatalogueFunction[] }) {
  return (
    <div className={`mt-4 ${TABLE_FRAME}`}>
      <table className="w-full text-left text-sm">
        <thead>
          <tr>
            <th className={TH}>Key</th>
            <th className={TH}>Interface</th>
            <th className={TH}>Instructions</th>
            <th className={TH}>Labels</th>
          </tr>
        </thead>
        <tbody>
          {functions.map((fn) => (
            <Fragment key={fn.id}>
              <tr>
                <td className={`${TD} font-mono text-xs text-text`}>{fn.key}</td>
                <td className={`${TD} font-mono text-xs text-soft`}>{fn.interface}</td>
                <td className={`${TD} text-sm text-soft`}>{fn.instructions}</td>
                <td className={`${TD} text-sm text-soft`}>{labelsText(fn.labels)}</td>
              </tr>
              {(fn.context !== null || fn.input !== null) && (
                <tr>
                  <td colSpan={4} className="border-b border-rule bg-background px-4 py-2 text-xs text-mute">
                    Uses its own request shape, not the adapter&apos;s:
                    {fn.input !== null && <> input <span className="font-mono">{fieldName(fn.input.codeName, fn.input.key)}</span> ({fn.input.description}).</>}
                    {fn.context !== null && <> context: {contextText(fn.context)}.</>}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
