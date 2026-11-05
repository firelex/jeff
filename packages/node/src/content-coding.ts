/** An HTTP answer's body read as the Python SDK's httpx reads it: at most MAX_ANSWER_BODY bytes as sent, then its
 * Content-Encoding undone as httpx undoes it (gzip and deflate, any number of layers, the last applied first; br,
 * zstd, x-gzip and codings httpx does not know are passed over, as httpx passes them over when their Python packages
 * are not installed, which the Python SDK does not require), each layer held to the same limit, so a few kilobytes of
 * stacked gzip cannot decode to gigabytes. Each layer is decoded by Node's zlib, as Python's zlib module decodes it
 * (asynchronously, on zlib's thread pool, layer after layer: there is no cap on the number of layers, as httpx has
 * none),
 * and a body that is not its coding is refused with zlib's error as Python words it. */

import { promisify } from "node:util";
import { constants, crc32, inflate as inflateZlib, inflateRaw } from "node:zlib";

/** zlib's asynchronous inflate, so that decoding many layers (or a large one) never holds up the event loop. */
const inflateAsync = promisify(inflateZlib) as unknown as (input: Buffer, options: object) => Promise<unknown>;
const inflateRawAsync = promisify(inflateRaw) as unknown as (input: Buffer, options: object) => Promise<unknown>;

/** The longest answer body this client reads, as sent and once each Content-Encoding layer is undone: 64 MiB (the
 * channel's frame limit, the same in every Jeff SDK). httpx has no limit; a Jeff answer is far smaller (a /v1/decide
 * answer of 1,000 calls is a few MiB). */
export const MAX_ANSWER_BODY = 64 * 1024 * 1024;

/** What this client accepts, as httpx's default header says. */
export const ACCEPT_ENCODING = "gzip, deflate";

/** A body, or one of its decoded layers, longer than the limit. */
export class BodyTooLarge extends Error {
  /** `decoded`: the body as sent was within the limit and undoing an encoding passed it. */
  constructor(readonly decoded: boolean) {
    super(`the answer body is larger than ${MAX_ANSWER_BODY} bytes${decoded ? " once decoded" : ""}`);
  }
}

/** A body that is not what its Content-Encoding says (httpx's DecodingError), with zlib's message as Python gives it. */
export class DecodingError extends Error {}

/** The body with every Content-Encoding httpx knows undone (`contentEncoding`: the header's value, several headers
 * joined by ", "; null when there is none), each layer held to `limit` bytes. `signal`, when given, is checked before
 * each layer: once it is aborted, decoding stops and its reason is thrown (RestTransport aborts it on close()). */
export async function decodedBody(raw: Buffer, contentEncoding: string | null, limit = MAX_ANSWER_BODY,
  signal?: AbortSignal): Promise<Buffer> {
  const codings = (contentEncoding ?? "").split(",").map((coding) => coding.trim().toLowerCase())
    .filter((coding) => coding === "gzip" || coding === "deflate");
  let body = raw;
  for (const coding of codings.reverse()) {
    signal?.throwIfAborted();
    body = coding === "gzip" ? await gunzip(body, limit) : await deflate(body, limit);
  }
  signal?.throwIfAborted();
  return body;
}

/** Python's zlib.error text for a zlib failure ("Error -3 while decompressing data: incorrect header check"). */
function zlibError(error: unknown): Error {
  const code = error !== null && typeof error === "object" ? (error as { code?: unknown; errno?: unknown }) : {};
  if (code.code === "ERR_BUFFER_TOO_LARGE") return new BodyTooLarge(true);
  if (code.errno === constants.Z_NEED_DICT) return new DecodingError("Error 2 while decompressing data");
  if (typeof code.errno === "number" && error instanceof Error) {
    return new DecodingError(`Error ${code.errno} while decompressing data: ${error.message}`);
  }
  throw error; // not a zlib failure: a bug, not the server's bytes
}

/** One zlib or raw deflate stream inflated until it ends (bytes after it are passed over) or the input runs out (a
 * stream cut short gives what was decoded, as zlib's decompressobj reports no error for one), at most `limit` bytes
 * of output; with how many input bytes it used. */
async function inflate(input: Buffer, raw: boolean, limit: number): Promise<{ output: Buffer; used: number }> {
  const options = { finishFlush: constants.Z_SYNC_FLUSH, maxOutputLength: limit, info: true } as const;
  try {
    const result = await (raw ? inflateRawAsync : inflateAsync)(input, options) as
      { buffer: Buffer; engine: { bytesWritten: number } };
    return { output: result.buffer, used: result.engine.bytesWritten };
  } catch (error) {
    throw zlibError(error);
  }
}

/** As httpx's DeflateDecoder: zlib-wrapped deflate, else raw deflate (whose failure is the one reported). */
async function deflate(body: Buffer, limit: number): Promise<Buffer> {
  try {
    return (await inflate(body, false, limit)).output;
  } catch (error) {
    if (!(error instanceof DecodingError)) throw error;
    return (await inflate(body, true, limit)).output;
  }
}

function broken(message: string): DecodingError {
  return new DecodingError(`Error -3 while decompressing data: ${message}`);
}

/** The index after the zero byte ending a header field from `at`; -1 when there is none. */
function afterZero(body: Buffer, at: number): number {
  if (at < 0) return at;
  const zero = body.indexOf(0, at);
  return zero < 0 ? -1 : zero + 1;
}

/** As zlib.decompressobj(MAX_WBITS | 16), which httpx's GZipDecoder uses: one gzip member (bytes after it are passed
 * over), its header and trailer checked with zlib's messages; a member cut short gives what was decoded. */
async function gunzip(body: Buffer, limit: number): Promise<Buffer> {
  const nothing = Buffer.alloc(0);
  if (body.length === 0) return body;
  if (body[0] !== 0x1f || (body.length >= 2 && body[1] !== 0x8b)) throw broken("incorrect header check");
  if (body.length < 10) return nothing;
  if (body[2] !== 8) throw broken("unknown compression method");
  const flags = body[3] as number;
  if ((flags & 0xe0) !== 0) throw broken("unknown header flags set");
  let at = 10;
  if ((flags & 0x04) !== 0) {
    if (at + 2 > body.length) return nothing;
    at += 2 + body.readUInt16LE(at);
  }
  if ((flags & 0x08) !== 0) at = afterZero(body, at);
  if ((flags & 0x10) !== 0) at = afterZero(body, at);
  if (at < 0 || at > body.length) return nothing;
  if ((flags & 0x02) !== 0) {
    if (at + 2 > body.length) return nothing;
    if ((crc32(body.subarray(0, at)) & 0xffff) !== body.readUInt16LE(at)) throw broken("header crc mismatch");
    at += 2;
  }
  const { output, used } = await inflate(body.subarray(at), true, limit);
  const trailer = at + used;
  // no complete trailer: the member was cut short (or ends exactly at the input's end without one)
  if (trailer + 8 > body.length) return output;
  if (body.readUInt32LE(trailer) !== crc32(output)) throw broken("incorrect data check");
  if (body.readUInt32LE(trailer + 4) !== output.length % 2 ** 32) throw broken("incorrect length check");
  return output;
}
