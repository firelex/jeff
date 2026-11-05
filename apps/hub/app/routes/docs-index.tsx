import { redirect } from 'react-router';

// `/docs` itself always redirects to the first guide; there is no "/docs" page of its own. Prerendered as a static
// redirect page (react-router.config.ts lists it alongside '/').
export function loader() {
  throw redirect('/docs/guides/introduction');
}

export default function DocsIndex() {
  return null;
}
