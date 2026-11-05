// The visitor's address. Vercel sets X-Forwarded-For to the client's own address, one value (it replaces any value
// the client sent); off Vercel the local server (scripts/local-server.ts) and the tests set it. Anything but exactly
// one valid IPv4 or IPv6 address is refused (a 400 naming the header), never read as some shared visitor: a request
// with no usable address would otherwise share one rate-limit count with every other such request (review N2).
import { isIP } from 'node:net';
import { ApiError } from '../../server/http.js';

export function clientIp(headers: Headers): string {
  const forwarded = headers.get('x-forwarded-for');
  if (forwarded === null || isIP(forwarded) === 0) {
    throw new ApiError(400, 'ClientAddressRefused',
      `X-Forwarded-For must be exactly one IP address, the visitor's; got ${forwarded === null ? 'none' : JSON.stringify(forwarded)}`,
      { header: 'x-forwarded-for' });
  }
  return forwarded;
}
