// Whether the signed-in visitor is the owner, learned in the browser after hydration from GET /api/admin/owner
// ({"owner": true} or false; server/endpoints/admin-adapters.ts), so no built file carries the owner's email
// and the first render matches the prerendered markup (false). The catalogue then shows the owner its open/API-only
// tags and the open adapters' green cards, which the public does not see. One request per page load, shared by every
// component that asks; a failed request counts as not the owner, and is reported with console.error.
import { useEffect, useState } from 'react';
import { askHub } from './ask-hub';

let asked: Promise<boolean> | null = null;

async function askOwner(): Promise<boolean> {
  const answer = await askHub('/api/admin/owner', null);
  if (answer.kind === 'answer' && answer.status === 200 && typeof (answer.body as { owner?: unknown } | null)?.owner === 'boolean') {
    return (answer.body as { owner: boolean }).owner;
  }
  console.error(`the owner check (GET /api/admin/owner) failed: ${answer.kind === 'failed' ? answer.reason : `status ${answer.status}`}`);
  return false;
}

export function useOwnerView(): boolean {
  const [owner, setOwner] = useState(false);
  useEffect(() => {
    let live = true;
    if (asked === null) asked = askOwner();
    void asked.then((value) => { if (live) setOwner(value); });
    return () => { live = false; };
  }, []);
  return owner;
}

// For tests: forget the answer, so the next page asks again.
export function forgetOwnerView(): void {
  asked = null;
}
