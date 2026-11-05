// The hub's one Vercel function (Vercel's file-based api/ folder, Node.js runtime, Web-standard fetch handler).
// vercel.json rewrites every other /api/* path here with an unnamed group (/api/(.*)), so the request arrives with
// its own path and query, unchanged, and server/router.ts picks the endpoint by that path. (A named parameter such as
// /api/:path* would add itself to the query: /api/a/b would arrive as /api/a/b?path=a%2Fb. Both were measured on a
// Vercel preview; test/local-server.test.ts pins them.) The site itself stays static files (build/client), which
// Vercel serves before any rewrite applies.
import { handle } from '../server/router.js';

export default { fetch: handle };
