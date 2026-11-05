// /privacy, prerendered from content/legal/privacy.md (app/components/LegalPage.tsx says how it is shown).
import { useLoaderData } from 'react-router';
import { LegalPage } from '../components/LegalPage.js';
import { legalPage } from '../lib/legal.server.js';
import type { Route } from './+types/privacy';

export function loader() {
  return legalPage('privacy');
}

export function meta({ loaderData }: Route.MetaArgs) {
  return [{ title: `${loaderData.title} | Jeff` }, { name: 'description', content: 'What Jeff collects, why, where it is kept and for how long.' }];
}

export default function Page() {
  const page = useLoaderData<typeof loader>();
  return <LegalPage {...page} />;
}
