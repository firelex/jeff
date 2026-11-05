// /terms, prerendered from content/legal/terms.md (app/components/LegalPage.tsx says how it is shown).
import { useLoaderData } from 'react-router';
import { LegalPage } from '../components/LegalPage.js';
import { legalPage } from '../lib/legal.server.js';
import type { Route } from './+types/terms';

export function loader() {
  return legalPage('terms');
}

export function meta({ loaderData }: Route.MetaArgs) {
  return [{ title: `${loaderData.title} | Jeff` }, { name: 'description', content: 'The terms for using Jeff, its API and its website.' }];
}

export default function Page() {
  const page = useLoaderData<typeof loader>();
  return <LegalPage {...page} />;
}
