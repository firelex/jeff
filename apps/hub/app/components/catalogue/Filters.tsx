// The catalogue list's search box and two selects (interface, domain). Filter state lives in the URL query
// (catalogue.tsx reads and writes it), so a static page loaded at e.g. /catalogue?domain=safety renders filtered as
// soon as this component sees that filter -- there is no separate client-only step. `filterEntries` is the pure
// part (unit tested); `Filters` is just the controlled inputs.
import { Input, Select } from '@jeff/ui';
import type { CatalogueEntry } from '../../lib/catalogue.server';

export interface CatalogueFilter {
  query: string;
  interface: string | null;
  domain: string | null;
}

export function filterEntries(entries: CatalogueEntry[], filter: CatalogueFilter): CatalogueEntry[] {
  const q = filter.query.trim().toLowerCase();
  return entries.filter((entry) => {
    if (filter.domain !== null && entry.domain !== filter.domain) return false;
    if (filter.interface !== null && !entry.functions.some((fn) => fn.interface === filter.interface)) return false;
    if (q !== '' && !`${entry.id} ${entry.name} ${entry.title} ${entry.summary}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

export function Filters({ entries, filter, onChange }: {
  entries: CatalogueEntry[];
  filter: CatalogueFilter;
  onChange: (filter: CatalogueFilter) => void;
}) {
  const domains = [...new Set(entries.map((e) => e.domain))].sort();
  const interfaces = [...new Set(entries.flatMap((e) => e.functions.map((fn) => fn.interface)))].sort();
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[2fr_1fr_1fr]">
      <Input
        type="search"
        placeholder="Search adapters"
        aria-label="Search adapters"
        value={filter.query}
        onChange={(e) => onChange({ ...filter, query: e.target.value })}
      />
      <Select
        aria-label="Interface"
        value={filter.interface ?? ''}
        onChange={(e) => onChange({ ...filter, interface: e.target.value === '' ? null : e.target.value })}
      >
        <option value="">All interfaces</option>
        {interfaces.map((i) => (
          <option key={i} value={i}>{i}</option>
        ))}
      </Select>
      <Select
        aria-label="Domain"
        value={filter.domain ?? ''}
        onChange={(e) => onChange({ ...filter, domain: e.target.value === '' ? null : e.target.value })}
      >
        <option value="">All domains</option>
        {domains.map((d) => (
          <option key={d} value={d}>{d}</option>
        ))}
      </Select>
    </div>
  );
}
