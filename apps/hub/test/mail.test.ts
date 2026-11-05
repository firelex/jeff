// @vitest-environment node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, it, vi } from 'vitest';
import { adapterRequestEmail, mailerFor, parseAddress, SENDGRID_MAIL_SEND, signInEmail } from '../app/lib/mail.server';

it('writes the sign-in email in plain English, with the link and how long it works', () => {
  const mail = signInEmail('ada@example.com', 'https://jeffhub.ai/api/auth/magic-link/verify?token=t');
  expect(mail.subject).toBe('Your Jeff sign-in link');
  expect(mail.text).toContain('https://jeffhub.ai/api/auth/magic-link/verify?token=t');
  expect(mail.text).toContain('It works once, within 15 minutes.');
  expect(mail.html).toContain('href="https://jeffhub.ai/api/auth/magic-link/verify?token=t"');
});

it('escapes the link in the HTML email', () => {
  const mail = signInEmail('ada@example.com', 'https://jeffhub.ai/x?a=1&b="2"');
  expect(mail.html).toContain('href="https://jeffhub.ai/x?a=1&amp;b=&quot;2&quot;"');
});

it('the test mailbox writes each email as a JSON file', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mailbox-'));
  await mailerFor({ transport: 'test-mailbox', dir, from: 'Jeff <no-reply@jeffhub.ai>' }, fetch)(signInEmail('a@example.com', 'https://x'));
  const files = fs.readdirSync(dir);
  expect(files).toHaveLength(1);
  expect(JSON.parse(fs.readFileSync(path.join(dir, files[0]), 'utf8'))).toMatchObject({ to: 'a@example.com',
    from: 'Jeff <no-reply@jeffhub.ai>', subject: 'Your Jeff sign-in link' });
});

// A fake fetch standing in for SendGrid: these tests never reach the real service.
function fakeSendGrid(status: number, body: string) {
  return vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(body, { status }));
}

const SENDGRID = { transport: 'sendgrid', apiKey: 'SG.secret-key-value', from: 'Jeff <no-reply@jeffhub.ai>' } as const;

it('sends through SendGrid\'s v3 Mail Send API with the key as a bearer token', async () => {
  const fake = fakeSendGrid(202, '');
  await mailerFor(SENDGRID, fake)(signInEmail('ada@example.com', 'https://jeffhub.ai/x'));
  expect(fake).toHaveBeenCalledTimes(1);
  const [url, init] = fake.mock.calls[0];
  expect(url).toBe(SENDGRID_MAIL_SEND);
  expect(SENDGRID_MAIL_SEND).toBe('https://api.sendgrid.com/v3/mail/send');
  expect(init?.method).toBe('POST');
  expect(init?.headers).toEqual({ Authorization: 'Bearer SG.secret-key-value', 'Content-Type': 'application/json' });
  const body = JSON.parse(String(init?.body));
  expect(body).toEqual({
    personalizations: [{ to: [{ email: 'ada@example.com' }] }],
    from: { name: 'Jeff', email: 'no-reply@jeffhub.ai' },
    subject: 'Your Jeff sign-in link',
    content: [{ type: 'text/plain', value: expect.stringContaining('https://jeffhub.ai/x') },
      { type: 'text/html', value: expect.stringContaining('href="https://jeffhub.ai/x"') }],
    // The sign-in link must reach the reader as written, never rewritten to a tracking redirect.
    tracking_settings: {
      click_tracking: { enable: false, enable_text: false },
      open_tracking: { enable: false },
      subscription_tracking: { enable: false },
    },
  });
});

it('a refused send throws, naming the status and SendGrid\'s error text, never the key', async () => {
  const answer = JSON.stringify({ errors: [{ message: 'The from address does not match a verified Sender Identity.', field: 'from' }] });
  const send = mailerFor(SENDGRID, fakeSendGrid(403, answer));
  const failure = await send(signInEmail('ada@example.com', 'https://x')).then(() => null, (e: unknown) => e as Error);
  expect(failure).toBeInstanceOf(Error);
  expect(failure!.message).toContain('SendGrid did not send the email to ada@example.com');
  expect(failure!.message).toContain('status 403');
  expect(failure!.message).toContain('The from address does not match a verified Sender Identity.');
  expect(failure!.message).not.toContain('SG.secret-key-value');
});

it.each([400, 401, 429, 500])('a %i answer from SendGrid throws', async (status) => {
  await expect(mailerFor(SENDGRID, fakeSendGrid(status, '{"errors":[{"message":"no"}]}'))(signInEmail('a@example.com', 'https://x')))
    .rejects.toThrow(`status ${status}`);
});

it('reads MAIL_FROM as a named or bare address, and refuses anything else', () => {
  expect(parseAddress('Jeff <no-reply@jeffhub.ai>')).toEqual({ name: 'Jeff', email: 'no-reply@jeffhub.ai' });
  expect(parseAddress('no-reply@jeffhub.ai')).toEqual({ email: 'no-reply@jeffhub.ai' });
  expect(() => parseAddress('Jeff')).toThrow('MAIL_FROM');
});

it("tells the owner about an adapter request, escaping the requester's words in the HTML", () => {
  const mail = adapterRequestEmail('notify@example.com', { id: 'AR-ABCDEFGH', requester: 'ada@example.com', question: 'Is <b>this</b> & that urgent?',
    kind: 'yes_no', optionCount: 2, examples: { source: 'generate', count: 1234 }, adminUrl: 'https://jeffhub.ai/admin/requests?id=AR-ABCDEFGH' });
  expect(mail.to).toBe('notify@example.com');
  expect(mail.subject).toBe('Adapter request AR-ABCDEFGH');
  expect(mail.text).toContain('Question: Is <b>this</b> & that urgent?\nKind of answer: Yes or no\nOptions: 2\nExamples: Generate the data for me\nTyped examples: 1,234');
  expect(mail.html).toContain('Is &lt;b&gt;this&lt;/b&gt; &amp; that urgent?');
  expect(mail.html).toContain('href="https://jeffhub.ai/admin/requests?id=AR-ABCDEFGH"');
});

it('says which way the examples were given: for a data set, the zip\'s size and its rows per option', () => {
  const mail = adapterRequestEmail('notify@example.com', { id: 'AR-ABCDEFGH', requester: 'ada@example.com', question: 'Which team?',
    kind: 'one', optionCount: 2, adminUrl: 'https://jeffhub.ai/admin/requests?id=AR-ABCDEFGH',
    examples: { source: 'data_set', bytes: 12_345_678, rows: 1500, counts: [{ key: 'billing', count: 700 }, { key: 'tech', count: 800 }] } });
  expect(mail.text).toContain('Options: 2\nExamples: I have a data set\nData set: 12.3 MB\nRows: 1,500\nRows per option: billing 700, tech 800\n');
});
