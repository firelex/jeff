import type { FunctionListing } from './api';

/** A function's area: the second part of its ID (jeff.<area>.<adapter>.<function>). */
export function areaOf(id: string): string {
  const parts = id.split('.');
  if (parts.length !== 4 || parts[0] !== 'jeff') throw new Error(`${id} is not a function ID of the form jeff.<area>.<adapter>.<function>`);
  return parts[1];
}

/** An adapter's area: the second part of its catalogue ID (jeff.<area>.<adapter>), one part shorter than a
 * function ID. */
export function adapterArea(id: string): string {
  const parts = id.split('.');
  if (parts.length !== 3 || parts[0] !== 'jeff') throw new Error(`${id} is not an adapter ID of the form jeff.<area>.<adapter>`);
  return parts[1];
}

/** The adapter a function belongs to: its ID without the last part. */
export function adapterOf(id: string): string {
  return id.slice(0, id.lastIndexOf('.'));
}

export function byArea(functions: FunctionListing[]): [string, FunctionListing[]][] {
  const groups = new Map<string, FunctionListing[]>();
  for (const listing of [...functions].sort((a, b) => a.id.localeCompare(b.id))) {
    const area = areaOf(listing.id);
    groups.set(area, [...(groups.get(area) ?? []), listing]);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

export function areaTitle(area: string): string {
  return area.charAt(0).toUpperCase() + area.slice(1);
}
