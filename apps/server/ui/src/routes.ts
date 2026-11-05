// Hash routes: #home, #playground, #factory, #adapters, #api and #access. An empty hash is the home page.
// Documentation is no longer one of them: the rail's "Documentation" item links out to the public hub's docs
// instead of a view here, so an address that used to be a docs page (#docs, #docs/<page>) is not found.

export type View = 'home' | 'playground' | 'factory' | 'adapters' | 'api' | 'access';

export type Route =
  | { view: View }
  | { view: 'not-found'; hash: string };

const PLAIN_VIEWS: View[] = ['home', 'playground', 'factory', 'adapters', 'api', 'access'];

export function parseHash(hash: string): Route {
  const address = hash.startsWith('#') ? hash.slice(1) : hash;
  if (address === '') return { view: 'home' };
  const plain = PLAIN_VIEWS.find((view) => view === address);
  if (plain) return { view: plain };
  return { view: 'not-found', hash: `#${address}` };
}

export function routeHash(route: Route): string {
  if (route.view === 'not-found') return route.hash;
  return `#${route.view}`;
}

/** The view name POST /ui/events records for a route (it accepts ^[a-z0-9-]+$ only). */
export function eventView(route: Route): string {
  return route.view;
}
