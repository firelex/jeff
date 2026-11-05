// Where the adapter requests' data sets are kept: a private Vercel Blob store. The browser uploads a zip straight to
// it (a Vercel function takes at most about 4.5 MB a request) with a client token the hub's function issues
// (server/endpoints/adapter-requests.ts, through @vercel/blob's handleUpload); the function then only looks a zip up,
// deletes it, or makes the owner a link to download it that works for a few minutes. Private blobs need the store's
// token to read, so a zip's URL alone opens nothing.
//
// The store's read-write token is the setting BLOB_READ_WRITE_TOKEN (Vercel sets it when the store is connected to
// the project). It is read when the store is used, and a missing one is an error naming it; a production build
// refuses to start without it (settings.server.ts).
import { BlobNotFoundError, del, head, issueSignedToken, list, presignUrl } from '@vercel/blob';
import { DATA_SET_ROOT } from './adapter-request.js';
import { requireEnv } from './env.server.js';

export const BLOB_TOKEN_SETTING = 'BLOB_READ_WRITE_TOKEN';
// A private blob's URL is on <store>.private.blob.vercel-storage.com; a public one's on <store>.public.blob....
export const PRIVATE_BLOB_HOST_SUFFIX = '.private.blob.vercel-storage.com';
// How long the owner's download link works.
export const DOWNLOAD_LINK_SECONDS = 5 * 60;

export interface StoredBlob { pathname: string; url: string; size: number }
export interface ListedBlob { pathname: string; uploadedAt: Date }

export interface DataSetStore {
  // The store's read-write token, which signs the browser's client token (handleUpload).
  readWriteToken(): string;
  // The blob at `pathname`, or null when there is none.
  find(pathname: string): Promise<StoredBlob | null>;
  remove(pathnames: string[]): Promise<void>;
  // Every blob whose pathname begins with `prefix` (all pages of the store's listing).
  list(prefix: string): Promise<ListedBlob[]>;
  // A link that downloads the blob until `validUntil` (milliseconds since 1970).
  downloadUrl(pathname: string, validUntil: number): Promise<string>;
}

// An account's folder in the store: adapter-requests/<the user's id>/.
export function dataSetFolderOf(userId: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(userId)) throw new Error(`the user id ${JSON.stringify(userId)} cannot name a folder of the data set store`);
  return `${DATA_SET_ROOT}/${userId}/`;
}

// Whether a blob's URL is a private one.
export function isPrivateBlobUrl(url: string): boolean {
  return URL.canParse(url) && new URL(url).protocol === 'https:' && new URL(url).hostname.endsWith(PRIVATE_BLOB_HOST_SUFFIX);
}

const token = () => requireEnv([BLOB_TOKEN_SETTING])[BLOB_TOKEN_SETTING];

export const liveDataSetStore: DataSetStore = {
  readWriteToken: token,
  async find(pathname) {
    try {
      const found = await head(pathname, { token: token() });
      return { pathname: found.pathname, url: found.url, size: found.size };
    } catch (error) {
      if (error instanceof BlobNotFoundError) return null;
      throw error;
    }
  },
  async remove(pathnames) {
    if (pathnames.length > 0) await del(pathnames, { token: token() });
  },
  async list(prefix) {
    const found: ListedBlob[] = [];
    let cursor: string | undefined;
    for (;;) {
      const page = await list({ prefix, cursor, limit: 1000, token: token() });
      for (const blob of page.blobs) found.push({ pathname: blob.pathname, uploadedAt: blob.uploadedAt });
      if (!page.hasMore) return found;
      if (page.cursor === undefined) throw new Error(`the file store's listing of ${prefix} says it has more, but gave no cursor`);
      cursor = page.cursor;
    }
  },
  async downloadUrl(pathname, validUntil) {
    const signed = await issueSignedToken({ token: token(), pathname, operations: ['get'], validUntil });
    return (await presignUrl(signed, { operation: 'get', pathname, validUntil, access: 'private' })).presignedUrl;
  },
};
