import { type RouteConfig, index, route } from '@react-router/dev/routes';

export default [
  index('routes/home.tsx'),
  route('docs', 'routes/docs-index.tsx'),
  // Before 'docs/:tab/:id': both are two path segments under /docs, same shape as the generic docs route, so the
  // specific ones must be matched first.
  route('docs/api', 'routes/api-index.tsx'),
  route('docs/api/:operation', 'routes/api-operation.tsx'),
  route('docs/:tab/:id', 'routes/docs-page.tsx'),
  route('catalogue', 'routes/catalogue.tsx'),
  route('catalogue/:id', 'routes/catalogue-adapter.tsx'),
  route('pricing', 'routes/pricing.tsx'),
  // The terms of service and the privacy policy (content/legal/), which Google's sign-in consent screen links to.
  route('terms', 'routes/terms.tsx'),
  route('privacy', 'routes/privacy.tsx'),
  // Live runs of the worked examples: a shell that calls the hub's function (POST /api/playground/decide).
  route('playground', 'routes/playground.tsx'),
  // One page for both: /signup carries the pricing line. Sign-in itself is the hub's function, at /api/auth/*.
  route('signin', 'routes/signin.tsx', { id: 'signin' }),
  route('signup', 'routes/signin.tsx', { id: 'signup' }),
  // The signed-in customer's account: a shell that loads it from the hub's function (GET /api/account).
  route('account', 'routes/account.tsx'),
  // Back from Stripe Checkout: a shell that waits for the webhook (GET /api/account/status, the hub's function).
  route('account/return', 'routes/account-return.tsx'),
  // Ask for an adapter for your own decision: a shell that sends the form to the hub's function (POST /api/adapter-requests).
  route('request-adapter', 'routes/request-adapter.tsx'),
  // The owner's list of adapter requests (?id=<request number> for one): a shell over /api/admin/adapter-requests.
  route('admin/requests', 'routes/admin-requests.tsx'),
  // The owner's view of the hidden adapters: a shell over /api/admin/adapters.
  route('admin/adapters', 'routes/admin-adapters.tsx'),
] satisfies RouteConfig;
