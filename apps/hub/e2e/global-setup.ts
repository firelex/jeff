import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { OAuth2Server } from 'oauth2-mock-server';
import { JEFF_API_PORT, OIDC_IDENTITY_FILE, OIDC_PORT, PLAYGROUND_API_KEY, TURNSTILE_PORT } from '../playwright.config';

// The person the test OpenID provider signs in, read from OIDC_IDENTITY_FILE at each sign-in (a check writes it
// first with providerIdentity, e2e/accounts.ts). A sign-in with no identity written fails, naming the file.
function identity(): Record<string, unknown> {
  if (!fs.existsSync(OIDC_IDENTITY_FILE)) throw new Error(`the test OpenID provider has no identity: write ${OIDC_IDENTITY_FILE} first (providerIdentity)`);
  return JSON.parse(fs.readFileSync(OIDC_IDENTITY_FILE, 'utf8')) as Record<string, unknown>;
}

// Cloudflare Turnstile's server-side check, answered locally: the token "e2e-pass" passes (solved on 127.0.0.1, the
// host the checks serve the hub on), anything else fails.
export default async function globalSetup() {
  const turnstile = http.createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      const success = new URLSearchParams(body).get('response') === 'e2e-pass';
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify(success ? { success: true, hostname: '127.0.0.1' } : { success: false, 'error-codes': ['invalid-input-response'] }));
    });
  });
  await new Promise<void>((resolve) => turnstile.listen(TURNSTILE_PORT, '127.0.0.1', resolve));
  // A stand-in for the Jeff API the playground calls (PLAYGROUND_API_URL): POST /v1/decide with the playground's key
  // answers one decision for the call's function, the same each time; anything else is a Jeff error.
  const jeffApi = http.createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      response.setHeader('content-type', 'application/json');
      if (request.method !== 'POST' || request.url !== '/v1/decide' || request.headers.authorization !== `Bearer ${PLAYGROUND_API_KEY}`) {
        response.statusCode = 401;
        response.end(JSON.stringify({ error: { type: 'Unauthorised', message: 'the stand-in API takes only the playground\'s call', details: {} } }));
        return;
      }
      const call = (JSON.parse(body) as { calls: { function: string }[] }).calls[0];
      response.end(JSON.stringify({ decisions: [{ value: 'yes', confidence: 0.93, abstained: false, calibrated: true, interface: 'detect',
        function: call.function, version: '1.4.0', probabilities: { yes: 0.93, no: 0.07 }, score: null, route: [],
        decided_by: call.function, escalations: [], timing: {}, evidence_refs: [] }] }));
    });
  });
  await new Promise<void>((resolve) => jeffApi.listen(JEFF_API_PORT, '127.0.0.1', resolve));
  // The test OpenID provider the hub's function finds at AUTH_TEST_PROVIDER: its authorize endpoint sends the browser
  // straight back with a code, and its id_token and user info carry the identity the check wrote.
  fs.rmSync(path.dirname(OIDC_IDENTITY_FILE), { recursive: true, force: true });
  fs.mkdirSync(path.dirname(OIDC_IDENTITY_FILE), { recursive: true });
  const oidc = new OAuth2Server();
  await oidc.issuer.keys.generate('RS256');
  oidc.service.on('beforeTokenSigning', (token) => { Object.assign(token.payload, identity()); });
  oidc.service.on('beforeUserinfo', (userInfo) => { userInfo.body = identity(); });
  await oidc.start(OIDC_PORT, '127.0.0.1');
  return async () => {
    await new Promise((resolve) => turnstile.close(resolve));
    await new Promise((resolve) => jeffApi.close(resolve));
    await oidc.stop();
  };
}
