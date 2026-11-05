// The request header the sign-in page sends Cloudflare Turnstile's token in. Its own module, so the page's browser
// code never imports a .server module to read it.
export const TURNSTILE_HEADER_NAME = 'x-turnstile-token';
