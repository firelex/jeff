// GET /api/health: the hub's endpoints are up, and which build of the hub is running: the commit the build was made
// from, which scripts/hub-version.mjs writes into the function's generated content at build time
// (VERCEL_GIT_COMMIT_SHA on Vercel, `git rev-parse --short HEAD` elsewhere; final review I8). No setting is read.
import { HUB_VERSION } from '../../app/lib/hub-version.generated.js';
import { defineEndpoint } from '../endpoint.js';
import { json } from '../http.js';

export const health = defineEndpoint({
  settings: [],
  methods: {
    GET: () => json(200, { status: 'ok', version: HUB_VERSION }),
  },
});
