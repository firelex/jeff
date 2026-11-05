import type { AdapterRequestForm, Example } from '../../app/lib/adapter-request';

// A request that meets every rule: one option from three (billing, tech, none), one text, a context field, 30
// examples (10 per option). Tests change one thing at a time.
export const TEAMS = ['billing', 'tech', 'none'] as const;

export function anExample(i: number, answer: string = TEAMS[i % 3]): Example {
  return { input: `Support email number ${i}: something about ${answer}.`, context: { product: 'Widget' }, answer, reason: '', tricky: false };
}

export function aForm(): AdapterRequestForm {
  return {
    decision: { question: 'Which team should handle this support email?', why: 'Emails reach the right team faster.' },
    answer: {
      kind: 'one',
      options: [
        { key: 'billing', definition: 'Payments, invoices and refunds.' },
        { key: 'tech', definition: 'Bugs and errors in the product.' },
        { key: 'none', definition: 'Neither team.' },
      ],
      noneFits: 'none_option',
      exampleSets: [],
    },
    input: { form: 'text', fields: [], typicalLength: 400, maxLength: 4000, languages: 'English' },
    context: [{ name: 'product', description: 'The product the email is about.', example: 'Widget' }],
    source: 'generate',
    examples: Array.from({ length: 30 }, (_, i) => anExample(i)),
    dataSet: null,
    moreExamplesLater: 500,
    use: { callsPerDay: 2000, speedMatters: false, accuracy: 'Right 95 times in 100', abstainBetter: true },
    rights: { mayShare: true, noPersonalData: true, mayImproveBase: false },
  };
}

// The same request with a data set instead of typed examples: 330 rows, 110 per option, uploaded to u1's folder.
export const DATA_SET_PATHNAME = 'adapter-requests/u1/data-set-Abc123.zip';
export function aDataSetForm(): AdapterRequestForm {
  return { ...aForm(), source: 'data_set', examples: [],
    dataSet: { pathname: DATA_SET_PATHNAME, bytes: 2_500_000, rows: 330, counts: TEAMS.map((key) => ({ key, count: 110 })) } };
}

// The form with `change` applied to a copy (a function that edits it in place).
export function aFormWith(change: (form: AdapterRequestForm) => void): AdapterRequestForm {
  const form = structuredClone(aForm());
  change(form);
  return form;
}
