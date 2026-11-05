import { Link, useLoaderData } from 'react-router';
import { DocsLayout } from '../components/docs/DocsLayout';
import { apiSidebar, operations } from '../lib/site.server';
import { CARD_LINK, PAGE_TITLE, SECTION_HEADING } from '../components/styles';
import { Tag } from '../components/Tag';

export function loader() {
  return { operations: operations(), sidebar: apiSidebar() };
}

export function meta() {
  return [{ title: 'API reference | Jeff docs' }];
}

export default function ApiIndex() {
  const { operations: ops, sidebar } = useLoaderData<typeof loader>();
  const groups = new Map<string, typeof ops>();
  for (const op of ops) {
    const group = groups.get(op.group) ?? [];
    group.push(op);
    groups.set(op.group, group);
  }
  return (
    <DocsLayout tab="api" sidebar={{ api: sidebar }} current="" headings={[]}>
      <h1 className={PAGE_TITLE}>API reference</h1>
      <p className="mt-4 text-sm leading-7 text-soft">Every operation Jeff's HTTP API serves publicly.</p>
      {[...groups.entries()].map(([group, groupOps]) => (
        <section key={group} className="mt-10">
          <h2 className={`border-b border-ink pb-2 ${SECTION_HEADING}`}>{group}</h2>
          <ul className="mt-4 space-y-3">
            {groupOps.map((op) => (
              <li key={op.id}>
                <Link to={`/docs/api/${op.id}`} className={`block px-4 py-3 ${CARD_LINK}`}>
                  <span className="flex items-center gap-2"><Tag tone={op.method === 'GET' ? 'ink' : 'accent'}>{op.method}</Tag><span className="font-mono text-xs text-mute">{op.path}</span></span>
                  <p className="mt-1 text-sm text-text">{op.summary}</p>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </DocsLayout>
  );
}
