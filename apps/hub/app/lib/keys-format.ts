// The API key form (spec Section 5): jeff_live_ followed by 32 random bytes in base32 (RFC 4648's alphabet in lower
// case, no padding): 52 characters. The 6 characters after the prefix are the key's visible prefix.
export const KEY_PREFIX = 'jeff_live_';
// At most this many live keys per account (spec Section 5), each named in 1 to MAX_NAME_LENGTH characters.
export const MAX_LIVE_KEYS = 10;
export const MAX_NAME_LENGTH = 60;
export const KEY_BODY_LENGTH = 52;
export const VISIBLE_PREFIX_LENGTH = 6;
// A whole key: the prefix and 52 characters of the base32 alphabet.
export const KEY_PATTERN = /^jeff_live_[a-z2-7]{52}$/;

export function shownKey(prefix: string): string {
  return `${KEY_PREFIX}${prefix}…`;
}

const ALPHABET = 'abcdefghijklmnopqrstuvwxyz234567';

// RFC 4648 base32 in lower case, without padding.
export function base32(bytes: Uint8Array): string {
  let bits = 0;
  let value = 0;
  let out = '';
  for (const byte of bytes) {
    value = ((value << 8) | byte) & 0xffff;
    bits += 8;
    while (bits >= 5) {
      out += ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += ALPHABET[(value << (5 - bits)) & 31];
  return out;
}
