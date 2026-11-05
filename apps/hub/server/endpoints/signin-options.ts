// GET /api/signin-options: what the prerendered sign-in page needs to know at run time: whether the visitor is
// signed in already (then the page goes to the account), the sign-in methods the function offers (SIGN_IN_PROVIDERS,
// which the page compares with the list it was built with), and whether the test OpenID provider is on (tests and
// local runs only; AUTH_TEST_PROVIDER is refused in production).
import { ACCOUNTS_SETTING_NAMES, accountsSettings } from '../../app/lib/accounts-settings.server.js';
import { sessionUser } from '../../app/lib/session.server.js';
import { defineEndpoint } from '../endpoint.js';
import { json } from '../http.js';

export const signinOptions = defineEndpoint({
  settings: ACCOUNTS_SETTING_NAMES,
  methods: {
    GET: async (request) => {
      const settings = accountsSettings();
      return json(200, {
        signedIn: (await sessionUser(request)) !== null,
        providers: settings.signInProviders,
        testProvider: settings.testOAuthIssuer !== null,
      });
    },
  },
});
