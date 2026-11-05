import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import type { MailSettings } from './accounts-settings.server.js';
import { MAGIC_LINK_MINUTES } from './auth-shared.js';
import { ANSWER_KIND_LABELS, EXAMPLE_SOURCE_LABELS, megabytes, type AnswerKind } from './adapter-request.js';

export interface Mail { to: string; subject: string; text: string; html: string }
export type SendMail = (mail: Mail) => Promise<void>;

// SendGrid's v3 Mail Send API, the only address the sendgrid transport posts to.
export const SENDGRID_MAIL_SEND = 'https://api.sendgrid.com/v3/mail/send';

// SendGrid in production; the test mailbox (a folder of JSON files) for tests and local runs, refused in production by
// the settings. A send that fails throws, so the sign-in form shows the failure instead of "check your email".
export function mailerFor(settings: MailSettings, fetchImpl: typeof fetch): SendMail {
  if (settings.transport === 'sendgrid') {
    const { from, apiKey } = settings;
    const sender = parseAddress(from);
    return async (mail) => {
      const response = await fetchImpl(SENDGRID_MAIL_SEND, {
        method: 'POST',
        headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          personalizations: [{ to: [{ email: mail.to }] }],
          from: sender,
          subject: mail.subject,
          content: [{ type: 'text/plain', value: mail.text }, { type: 'text/html', value: mail.html }],
          // No click or open tracking, whatever the SendGrid account's defaults: click tracking rewrites the sign-in
          // link to a sendgrid.net redirect, which mail filters (Microsoft 365) read as phishing and send to junk, and
          // the tracking pixel adds to that. These emails are transactional; nothing about them is measured.
          tracking_settings: {
            click_tracking: { enable: false, enable_text: false },
            open_tracking: { enable: false },
            subscription_tracking: { enable: false },
          },
        }),
        signal: AbortSignal.timeout(10_000),
      });
      if (!response.ok) {
        // SendGrid's answer names what it refused ({ errors: [{ message, field }] }); the key travels only in the
        // Authorization header, so it is never part of this message.
        const text = (await response.text()).trim();
        throw new Error(`SendGrid did not send the email to ${mail.to}: status ${response.status}: ${text}`);
      }
    };
  }
  return async (mail) => {
    fs.mkdirSync(settings.dir, { recursive: true });
    const file = path.join(settings.dir, `${Date.now()}-${randomUUID()}.json`);
    fs.writeFileSync(file, JSON.stringify({ from: settings.from, ...mail }));
  };
}

// MAIL_FROM as SendGrid's sender: "Name <address>" or a bare address.
export function parseAddress(from: string): { email: string; name?: string } {
  const named = /^\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$/.exec(from);
  if (named !== null) return named[1] === '' ? { email: named[2] } : { name: named[1], email: named[2] };
  if (/^[^<>\s]+@[^<>\s]+$/.test(from.trim())) return { email: from.trim() };
  throw new Error(`MAIL_FROM must be an address or "Name <address>", not ${JSON.stringify(from)}`);
}

function escapeHtml(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}

export function signInEmail(to: string, url: string): Mail {
  const text = [
    'Here is your link to sign in to Jeff:',
    '',
    url,
    '',
    `It works once, within ${MAGIC_LINK_MINUTES} minutes. If you did not ask to sign in, you can ignore this email.`,
  ].join('\n');
  const html = `<p>Here is your link to sign in to Jeff:</p><p><a href="${escapeHtml(url)}">Sign in to Jeff</a></p>`
    + `<p>It works once, within ${MAGIC_LINK_MINUTES} minutes. If you did not ask to sign in, you can ignore this email.</p>`;
  return { to, subject: 'Your Jeff sign-in link', text, html };
}

// What the owner is told about a new adapter request: never its examples or rows, only what identifies it, how the
// examples were given and their size: for a data set the zip's size and its rows (in all and per option), for
// "Generate the data for me" the number of examples.
export interface RequestNotice {
  id: string; requester: string; question: string; kind: AnswerKind; optionCount: number; adminUrl: string;
  examples: { source: 'generate'; count: number } | { source: 'data_set'; bytes: number; rows: number; counts: { key: string; count: number }[] };
}

export function adapterRequestEmail(to: string, notice: RequestNotice): Mail {
  const { examples } = notice;
  const lines: [string, string][] = [
    ['Request number', notice.id],
    ['From', notice.requester],
    ['Question', notice.question],
    ['Kind of answer', ANSWER_KIND_LABELS[notice.kind]],
    ['Options', String(notice.optionCount)],
    ['Examples', EXAMPLE_SOURCE_LABELS[examples.source]],
    ...(examples.source === 'generate'
      ? [['Typed examples', examples.count.toLocaleString('en-US')] as [string, string]]
      : [
        ['Data set', megabytes(examples.bytes)] as [string, string],
        ['Rows', examples.rows.toLocaleString('en-US')] as [string, string],
        ...(examples.counts.length === 0 ? [] : [['Rows per option', examples.counts.map((c) => `${c.key} ${c.count.toLocaleString('en-US')}`).join(', ')] as [string, string]]),
      ]),
  ];
  const text = ['A new adapter request has arrived.', '', ...lines.map(([name, value]) => `${name}: ${value}`), '',
    `Open it: ${notice.adminUrl}`].join('\n');
  const html = '<p>A new adapter request has arrived.</p><table>'
    + lines.map(([name, value]) => `<tr><td>${escapeHtml(name)}</td><td>${escapeHtml(value)}</td></tr>`).join('')
    + `</table><p><a href="${escapeHtml(notice.adminUrl)}">Open the request</a></p>`;
  return { to, subject: `Adapter request ${notice.id}`, text, html };
}
