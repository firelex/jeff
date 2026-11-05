import { useLoaderData } from 'react-router';
import { DocsLayout } from '../components/docs/DocsLayout';
import { Operation } from '../components/Operation';
import { apiSidebar, operations } from '../lib/site.server';
import type { Route } from './+types/api-operation';

export function loader({ params }: Route.LoaderArgs) {
  const operation = operations().find((op) => op.id === params.operation);
  if (operation === undefined) throw new Response(`No API operation ${params.operation}`, { status: 404 });
  return { operation, sidebar: apiSidebar() };
}

export function meta({ loaderData }: Route.MetaArgs) {
  return [{ title: `${loaderData.operation.method} ${loaderData.operation.path} | Jeff docs` }];
}

export default function ApiOperation() {
  const { operation, sidebar } = useLoaderData<typeof loader>();
  return (
    <DocsLayout tab="api" sidebar={{ api: sidebar }} current={operation.id} headings={[]}>
      <Operation operation={operation} />
    </DocsLayout>
  );
}
