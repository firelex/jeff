// Uploading a checked data set's zip from the browser straight to the hub's private file store (Vercel Blob): the
// hub's function issues the upload token (POST /api/adapter-requests/upload, @vercel/blob's handleUpload on the
// server), and @vercel/blob's client put() sends the zip to the store with it. The token is asked for here rather
// than by the library's upload(), so a refusal (a limit reached, a file too large) reaches the page in the hub's own
// words instead of the library's "Failed to retrieve the client token".
import { put } from '@vercel/blob/client';
import { DATA_SET_CONTENT_TYPE, DATA_SET_NAME, type UploadToken } from './adapter-request';
import { askHub, refusalOf } from './ask-hub';

// Larger zips go up in parts, in parallel, each part retried on its own.
const MULTIPART_FROM_BYTES = 50_000_000;

export type Uploaded = { kind: 'uploaded'; pathname: string } | { kind: 'refused'; message: string };

export async function uploadDataSet(file: File, folder: string, onProgress: (percent: number) => void): Promise<Uploaded> {
  const pathname = `${folder}${DATA_SET_NAME}`;
  const multipart = file.size >= MULTIPART_FROM_BYTES;
  const answer = await askHub('/api/adapter-requests/upload', { body: {
    type: 'blob.generate-client-token',
    payload: { pathname, multipart, clientPayload: JSON.stringify({ contentType: DATA_SET_CONTENT_TYPE, bytes: file.size }) },
  } });
  if (answer.kind !== 'answer' || answer.status !== 200) return { kind: 'refused', message: refusalOf(answer) };
  const issued = answer.body as Partial<UploadToken> | null;
  if (issued === null || typeof issued.clientToken !== 'string' || typeof issued.pathname !== 'string') {
    throw new Error('the hub\'s upload endpoint answered 200 without a clientToken and a pathname');
  }
  const token = issued.clientToken;
  try {
    // The token may create only the pathname the hub picked for it (re-review R1).
    const stored = await put(issued.pathname, file, {
      access: 'private', token, contentType: DATA_SET_CONTENT_TYPE, multipart,
      onUploadProgress: ({ percentage }) => onProgress(Math.floor(percentage)),
    });
    return { kind: 'uploaded', pathname: stored.pathname };
  } catch (error) {
    // The library's own errors (its BlobError, which @vercel/blob/client does not export, says "Vercel Blob: ...")
    // and a failed send (a TypeError) are shown as they are; anything else is a bug and is thrown.
    const blobError = error instanceof Error && error.message.startsWith('Vercel Blob:');
    if (!blobError && !(error instanceof TypeError)) throw error;
    return { kind: 'refused', message: `The data set could not be uploaded (${(error as Error).message}). Try again.` };
  }
}
