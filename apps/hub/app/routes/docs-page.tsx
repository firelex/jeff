import { useLoaderData } from 'react-router';
import { DocsLayout } from '../components/docs/DocsLayout';
import { Markdown } from '../components/docs/Markdown';
import { docs } from '../lib/site.server';
import type { Route } from './+types/docs-page';

export async function loader({ params }: Route.LoaderArgs) {
  const { pages, sidebar } = docs();
  const page = pages.find((p) => p.tab === params.tab && p.id === params.id);
  if (page === undefined) throw new Response(`No docs page /docs/${params.tab}/${params.id}`, { status: 404 });
  return { page, sidebar };
}

export function meta({ loaderData }: Route.MetaArgs) {
  return [{ title: `${loaderData.page.title} | Jeff docs` }];
}

export default function DocsPage() {
  const { page, sidebar } = useLoaderData<typeof loader>();
  return (
    <DocsLayout tab={page.tab} sidebar={sidebar} current={page.id} headings={page.headings}>
      <p className="mb-2 font-mono text-xs uppercase tracking-wide text-mute">{page.group}</p>
      <Markdown body={page.body} benchmarks={page.benchmarks} />
    </DocsLayout>
  );
}
