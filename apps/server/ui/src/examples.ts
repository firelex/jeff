import type { Json, Question } from './api';

export interface BaseExample {
  id: string;
  title: string;
  description: string;
  state: string | { [key: string]: Json };
  questions: Record<string, Question>;
  needsImage?: boolean;
}

// Requests that work on the base model through POST /v1/systemone; the functions tab has each adapter's own examples (GET /ui/examples).
export const EXAMPLES: BaseExample[] = [
  {
    id: 'alert',
    title: 'Triage a security alert',
    description: 'Incident type, severity and whether to lock the account, from one alert',
    state: 'Mail gateway alert: at 09:12 a finance employee opened an email that claimed to be from payroll and typed her password into a page at payro11-acme.com. At 09:20 her account signed in from a device we have never seen, in a country where we have no staff.',
    questions: {
      incident: { type: 'choice', instructions: 'What kind of security incident is this?', criteria: { stolen_login: "Someone's password or sign-in was stolen", malware: 'Harmful software running on a device', data_leak: 'Company data sent somewhere it should not go', false_alarm: 'Normal activity; nothing to do' } },
      severity: { type: 'score', instructions: 'How serious is this for the company right now?', criteria: ['Low: no harm done', 'Medium: limited to one person', 'High: an attacker may already be inside'] },
      lock_now: { type: 'noul', instructions: 'The account should be locked straight away, before anyone looks into it further.' },
    },
  },
  {
    id: 'review',
    title: 'Read a product review',
    description: 'Overall sentiment and whether a defect is described',
    state: 'The headphones sound wonderful and the battery lasts all day. The left ear cushion fell apart after a week.',
    questions: {
      sentiment: { type: 'choice', instructions: "What is the review's overall sentiment?", criteria: { positive: null, mixed: null, negative: null } },
      defect: { type: 'noul', instructions: 'Does the review describe a physical product defect?' },
    },
  },
  {
    id: 'screen',
    title: 'Watch a live screen',
    description: 'Structured state; the last field is the part that changes',
    state: { app: 'Order desk', customer_tier: 'gold', latest_message: 'Where is my parcel? It was due on Monday.' },
    questions: {
      intent: { type: 'choice', instructions: 'What does the latest message ask for?', criteria: { tracking: 'Where an order is', returns: 'Sending something back', billing: 'Charges and invoices', other: null } },
    },
  },
  {
    id: 'image',
    title: 'Look at an image',
    description: 'Attach a picture, then ask about what is in it',
    state: 'Use the attached image to answer the questions about the visible objects.',
    questions: {
      left_color: { type: 'choice', instructions: 'What is the color of the object on the left?', criteria: { red: null, blue: null, green: null, orange: null, other: null } },
    },
    needsImage: true,
  },
];

// Starting points for a new question, one per question type.
export const TEMPLATES: { type: Question['type']; title: string; hint: string; question: Question }[] = [
  { type: 'noul', title: 'Evaluate how true something is', hint: 'The sender wants a reply from us.', question: { type: 'noul', instructions: 'The sender wants a reply from us.' } },
  { type: 'score', title: 'Grade with a rubric', hint: 'How soon does the writer need us to act?', question: { type: 'score', instructions: 'How soon does the writer need us to act?', criteria: ['No deadline: it can wait', 'Within the next few days or this week', 'Today or immediately'] } },
  { type: 'choice', title: 'Ask a multiple-choice question', hint: 'Which desk owns this?', question: { type: 'choice', instructions: 'Which desk owns this?', criteria: { payments: 'Payments and transfers', security: 'Accounts and sign-ins', other: null } } },
];
