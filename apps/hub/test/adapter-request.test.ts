// @vitest-environment node
// The rules of an adapter request (app/lib/adapter-request.ts), which the page and the hub's function share: each
// rule refuses what breaks it, in words that name the field, and a request that meets every rule passes as it is.
import { describe, expect, it } from 'vitest';
import {
  bodyBytes, checkAdapterRequest, countsPerOption, LIMITS, meetsExampleMinimums, type AdapterRequestForm, type Problem,
} from '../app/lib/adapter-request';
import { aDataSetForm, aForm, aFormWith, anExample } from './helpers/adapter-request';

function problemsOf(form: unknown): Problem[] {
  const checked = checkAdapterRequest(form);
  if (checked.ok) throw new Error('expected the request to be refused');
  return checked.problems;
}
const messagesOf = (form: unknown) => problemsOf(form).map((p) => p.message);

it('passes a request that meets every rule, trimmed', () => {
  const form = aFormWith((f) => { f.decision.question = '  Which team should handle this support email?  '; });
  const checked = checkAdapterRequest(form);
  expect(checked.ok).toBe(true);
  if (checked.ok) expect(checked.form.decision.question).toBe('Which team should handle this support email?');
});

it('refuses something that is not an object', () => {
  expect(messagesOf([])).toEqual(["The request must be a JSON object with the form's eight sections."]);
  expect(messagesOf(null)).toHaveLength(1);
});

it('says when a whole section is missing', () => {
  const { use: _use, ...rest } = aForm();
  expect(messagesOf(rest)).toContain('Use: the section is missing.');
});

describe('1. the decision', () => {
  it.each([
    ['short', 'Too short', 'The question must be 10 to 300 characters; it has 9.'],
    ['long', 'x'.repeat(301), 'The question must be 10 to 300 characters; it has 301.'],
    ['empty', '', 'The question is required.'],
    ['two lines', 'Which team should\nhandle this?', 'The question must be on one line.'],
  ])('refuses a question that is %s', (_name, question, message) => {
    expect(messagesOf(aFormWith((f) => { f.decision.question = question; }))).toEqual([message]);
  });

  it('needs why it matters, up to 600 characters', () => {
    expect(messagesOf(aFormWith((f) => { f.decision.why = ' '; }))).toEqual(['Why it matters is required.']);
    expect(messagesOf(aFormWith((f) => { f.decision.why = 'x'.repeat(601); }))).toEqual(['Why it matters must be 1 to 600 characters; it has 601.']);
  });
});

describe('2 and 3. the kind of answer and the options', () => {
  it('needs a kind of answer from the list', () => {
    const problems = problemsOf(aFormWith((f) => { (f.answer as { kind: string }).kind = 'maybe'; }));
    expect(problems[0]).toMatchObject({ section: 2, field: 'answer.kind', message: 'The kind of answer: choose one.' });
  });

  it('needs 2 to 30 options', () => {
    const one = aFormWith((f) => { f.answer.options = [{ key: 'none', definition: 'x' }]; f.examples = []; });
    expect(messagesOf(one)).toContain('The options must have 2 to 30 options; it has 1.');
    const many = aFormWith((f) => { f.answer.options = Array.from({ length: 31 }, (_, i) => ({ key: i === 0 ? 'none' : `k${i}`, definition: 'x' })); });
    expect(messagesOf(many)).toContain('The options must have 2 to 30 options; it has 31.');
  });

  it('refuses keys that are not lower-case letters, digits and underscores, or used twice', () => {
    expect(messagesOf(aFormWith((f) => { f.answer.options[0].key = 'Billing'; })))
      .toContain('The options, option 1: the key "Billing" may use only lower-case letters, digits and underscores.');
    expect(messagesOf(aFormWith((f) => { f.answer.options[1].key = 'billing'; })))
      .toContain('The options, option 2: the key "billing" is used twice; every key must be different.');
  });

  it('needs a one-line definition for every option', () => {
    expect(messagesOf(aFormWith((f) => { f.answer.options[1].definition = ''; }))).toContain('The options, option 2: the definition is required.');
    expect(messagesOf(aFormWith((f) => { f.answer.options[1].definition = 'a\nb'; }))).toContain('The options, option 2: the definition must be on one line.');
  });

  it('needs what happens when no option fits, and a "none" option exactly when that is the choice', () => {
    expect(messagesOf(aFormWith((f) => { (f.answer as { noneFits: unknown }).noneFits = undefined; })))
      .toContain('What should happen when no option fits: choose one.');
    expect(messagesOf(aFormWith((f) => { f.answer.noneFits = 'abstain'; })))
      .toContain('The options has the option "none", but you chose to abstain when no option fits; remove it or choose a "none" option.');
    const noNone = aFormWith((f) => { f.answer.options[2].key = 'sales'; f.examples = f.examples.map((e) => (e.answer === 'none' ? { ...e, answer: 'sales' } : e)); });
    expect(messagesOf(noNone)).toEqual(['The options must include the option "none", since you chose a "none" option for when no option fits.']);
  });

  it('yes or no has exactly the options yes and no (and none, when chosen)', () => {
    const yesNo = (keys: string[], noneFits: 'abstain' | 'none_option') => aFormWith((f) => {
      f.answer = { kind: 'yes_no', options: keys.map((key) => ({ key, definition: `When ${key}` })), noneFits, exampleSets: [] };
      f.examples = Array.from({ length: 30 }, (_, i) => anExample(i, keys[i % keys.length]));
    });
    expect(checkAdapterRequest(yesNo(['yes', 'no'], 'abstain')).ok).toBe(true);
    expect(checkAdapterRequest(yesNo(['yes', 'no', 'none'], 'none_option')).ok).toBe(true);
    expect(messagesOf(yesNo(['yes', 'maybe'], 'abstain'))).toContain('A yes-or-no answer has exactly the options "yes", "no".');
  });

  it('options given by the caller need three example option sets and no fixed list', () => {
    const caller = aFormWith((f) => {
      f.answer = { kind: 'caller', options: [], noneFits: 'abstain', exampleSets: [
        [{ key: 'a', definition: 'A' }, { key: 'b', definition: 'B' }],
        [{ key: 'c', definition: 'C' }, { key: 'd', definition: 'D' }],
        [{ key: 'e', definition: 'E' }, { key: 'f', definition: 'F' }, { key: 'g', definition: 'G' }],
      ] };
      f.examples = Array.from({ length: 30 }, (_, i) => ({ ...anExample(i, 'x'), options: [{ key: 'x', definition: 'X' }, { key: 'y', definition: 'Y' }] }));
    });
    expect(checkAdapterRequest(caller).ok).toBe(true);
    expect(messagesOf({ ...caller, answer: { ...caller.answer, exampleSets: caller.answer.exampleSets.slice(0, 2) } }))
      .toContain('Give exactly 3 example option sets; there are 2.');
    expect(messagesOf({ ...caller, answer: { ...caller.answer, options: [{ key: 'a', definition: 'A' }] } }))
      .toContain('Options given by the caller have no fixed list; give three example option sets instead.');
    const noOptions = structuredClone(caller);
    delete noOptions.examples[4].options;
    expect(messagesOf(noOptions)).toEqual(['Example 5: the options it was asked with are required (options given by the caller).']);
    const wrongAnswer = structuredClone(caller);
    wrongAnswer.examples[0].answer = 'z';
    expect(messagesOf(wrongAnswer)).toEqual(['Example 1: the answer "z" is not one of the options.']);
  });

  it('the fixed kinds take no example option sets', () => {
    expect(messagesOf(aFormWith((f) => { f.answer.exampleSets = [[]]; }))).toContain('Example option sets are only for options given by the caller.');
  });
});

describe('4. the input', () => {
  it('needs a form, and named fields need 1 to 20 unique names that are not taken', () => {
    expect(messagesOf(aFormWith((f) => { (f.input as { form: unknown }).form = 'pdf'; }))).toContain('The form of the input: choose one.');
    expect(messagesOf(aFormWith((f) => { f.input.form = 'fields'; f.input.fields = []; }))).toContain('Named fields need 1 to 20 fields; there are 0.');
    expect(messagesOf(aFormWith((f) => { f.input.form = 'fields'; f.input.fields = [{ name: 'answer', description: 'x' }]; })))
      .toContain('Input field 1: the name "answer" is taken by the examples\' own columns; choose another.');
    expect(messagesOf(aFormWith((f) => { f.input.form = 'fields'; f.input.fields = [{ name: 'product', description: 'x' }]; })))
      .toContain('Context field 1: the name "product" is used twice; every input and context field needs its own name.');
    expect(messagesOf(aFormWith((f) => { f.input.fields = [{ name: 'body', description: 'x' }]; })))
      .toContain('An input of one text has no named fields; remove them or choose named fields.');
  });

  it('needs whole-number lengths, the typical one no more than the maximum, and the languages', () => {
    expect(messagesOf(aFormWith((f) => { f.input.maxLength = Number.NaN; }))).toContain('The maximum length must be a whole number from 1 to 100,000.');
    expect(messagesOf(aFormWith((f) => { f.input.typicalLength = 5000; }))).toContain('The typical length (5,000) is more than the maximum length (4,000).');
    expect(messagesOf(aFormWith((f) => { f.input.languages = ''; }))).toContain('The language or languages is required.');
  });

  it('named fields: every example gives each field, and nothing else', () => {
    const fields = aFormWith((f) => {
      f.input = { form: 'fields', fields: [{ name: 'subject', description: 'The subject' }, { name: 'body', description: 'The body' }], typicalLength: 10, maxLength: 200, languages: 'English' };
      f.examples = f.examples.map((e, i) => ({ ...e, input: { subject: `Subject ${i}`, body: 'Body' } }));
    });
    expect(checkAdapterRequest(fields).ok).toBe(true);
    const missing = structuredClone(fields);
    missing.examples[2].input = { subject: 'Only a subject' };
    expect(messagesOf(missing)).toEqual(['Example 3: the input field "body" is missing.']);
    const extra = structuredClone(fields);
    extra.examples[2].input = { subject: 'S', body: 'B', cc: 'x' };
    expect(messagesOf(extra)).toEqual(['Example 3: the input has a field "cc" that section 4 does not name.']);
  });
});

describe('5. context', () => {
  it('needs a name, a description and an example value for every field, at most 20', () => {
    expect(messagesOf(aFormWith((f) => { f.context[0].example = ''; }))).toContain('Context field 1: the example value is required.');
    expect(messagesOf(aFormWith((f) => { f.context[0].description = ''; }))).toContain('Context field 1: the description is required.');
    const many = aFormWith((f) => { f.context = Array.from({ length: 21 }, (_, i) => ({ name: `c${i}`, description: 'd', example: 'e' })); });
    expect(messagesOf(many)).toContain('Give at most 20 context fields; there are 21.');
  });

  it('every example gives every context value', () => {
    expect(messagesOf(aFormWith((f) => { f.examples[7].context = {}; }))).toEqual(['Example 8: the context value "product" is required.']);
    expect(messagesOf(aFormWith((f) => { f.examples[7].context = { product: 'W', other: 'x' }; })))
      .toEqual(['Example 8: the context has a field "other" that section 5 does not name.']);
  });
});

describe('6. examples', () => {
  it('needs at least 30 and at most 2,000', () => {
    expect(messagesOf(aFormWith((f) => { f.examples = f.examples.slice(0, 29); }))).toContain('Give at least 30 labelled examples; there are 29.');
    const many = aFormWith((f) => { f.examples = Array.from({ length: 2001 }, (_, i) => anExample(i)); });
    expect(messagesOf(many)).toEqual(['Give at most 2,000 examples; there are 2,001.']);
  });

  it('needs at least 3 for every option', () => {
    const few = aFormWith((f) => { f.examples = [...Array.from({ length: 28 }, (_, i) => anExample(i, i % 2 === 0 ? 'billing' : 'tech')), anExample(28, 'none'), anExample(29, 'none')]; });
    expect(messagesOf(few)).toEqual(['The option "none" has 2 examples; every option needs at least 3.']);
    expect(meetsExampleMinimums(few)).toBe(false);
    expect(meetsExampleMinimums(aForm())).toBe(true);
    expect(countsPerOption(few)).toEqual([{ key: 'billing', count: 14 }, { key: 'tech', count: 14 }, { key: 'none', count: 2 }]);
  });

  it('every answer is one of the options; several options take a list of them', () => {
    expect(messagesOf(aFormWith((f) => { f.examples[3].answer = 'sales'; }))).toEqual(['Example 4: the answer "sales" is not one of the options.']);
    expect(messagesOf(aFormWith((f) => { f.examples[3].answer = ''; }))).toEqual(['Example 4: the correct answer is required.']);
    const several = aFormWith((f) => { f.answer.kind = 'several'; f.examples = f.examples.map((e) => ({ ...e, answer: [e.answer as string] })); });
    expect(checkAdapterRequest(several).ok).toBe(true);
    several.examples[0].answer = ['billing', 'billing'];
    expect(messagesOf(several)).toEqual(['Example 1: the answer names one option twice.']);
    several.examples[0].answer = 'billing';
    expect(messagesOf(several)).toEqual(['Example 1: the answer must be one or more of the options.']);
  });

  it('an input is required and no longer than the maximum length', () => {
    expect(messagesOf(aFormWith((f) => { f.examples[0].input = ' '; }))).toEqual(['Example 1: the input text is required.']);
    expect(messagesOf(aFormWith((f) => { f.examples[0].input = 'x'.repeat(4001); })))
      .toEqual(['Example 1: the input has 4,001 characters, more than the maximum length of 4,000.']);
  });

  it('the reason is one optional line, and tricky is yes or no', () => {
    expect(messagesOf(aFormWith((f) => { f.examples[0].reason = 'x'.repeat(LIMITS.reasonMax + 1); }))).toEqual(['Example 1: the reason must be at most 300 characters.']);
    expect(messagesOf(aFormWith((f) => { (f.examples[0] as { tricky: unknown }).tricky = 'yes'; }))).toEqual(['Example 1: the tricky mark must be yes or no.']);
    expect(messagesOf(aFormWith((f) => { (f.examples[0] as unknown as Record<string, unknown>).label = 'x'; })))
      .toEqual(['Example 1: it has a field the form does not have: "label".']);
  });

  it('says a problem in the options once, not again for every example', () => {
    expect(messagesOf(aFormWith((f) => { f.answer.options[0].key = 'Bad Key'; }))).toHaveLength(1);
  });

  it('needs how many more examples could come later, as a whole number', () => {
    expect(messagesOf(aFormWith((f) => { f.moreExamplesLater = -1; })))
      .toEqual(['How many more real examples you could supply later must be a whole number from 0 to 10,000,000.']);
  });
});

describe('7. use and 8. data and rights', () => {
  it('needs the calls a day, both yes-or-no answers and the accuracy', () => {
    const form = aFormWith((f) => {
      f.use.callsPerDay = 1.5;
      (f.use as { speedMatters: unknown }).speedMatters = null;
      f.use.accuracy = '';
      (f.use as { abstainBetter: unknown }).abstainBetter = 'yes';
    });
    expect(messagesOf(form)).toEqual([
      'Calls a day must be a whole number from 0 to 1,000,000,000.',
      'Whether speed matters: choose yes or no.',
      'The accuracy that would make it useful is required.',
      'Whether no answer is better than a wrong one: choose yes or no.',
    ]);
  });

  it('needs both required ticks; the base-model tick is optional but must be given', () => {
    const form = aFormWith((f) => { f.rights.mayShare = false; f.rights.noPersonalData = false; });
    expect(messagesOf(form)).toEqual(['Tick that you have the right to share these examples.',
      'Tick that the examples contain no personal data, or that it has been removed.']);
    expect(checkAdapterRequest(aFormWith((f) => { f.rights.mayImproveBase = true; })).ok).toBe(true);
  });
});

it('refuses a field the form does not have', () => {
  expect(messagesOf({ ...aForm(), extra: 1 })).toEqual(['The request has a field the form does not have: "extra".']);
});

it('measures the request as it is sent, in bytes', () => {
  const form: AdapterRequestForm = aForm();
  expect(bodyBytes(form)).toBe(new TextEncoder().encode(JSON.stringify(form)).length);
  expect(bodyBytes({ q: 'é' })).toBe(10);
});

describe('6. how the examples are given', () => {
  it('needs a choice between a data set and generating the data', () => {
    const { source: _source, ...rest } = aForm();
    expect(messagesOf(rest)).toEqual(['How you give the examples: choose one.']);
  });

  it('passes a data set that meets the minimums', () => {
    const checked = checkAdapterRequest(aDataSetForm());
    expect(checked.ok).toBe(true);
    if (checked.ok) expect(checked.form.dataSet).toEqual(aDataSetForm().dataSet);
  });

  it('a data set has no typed examples, and typed examples no data set', () => {
    expect(messagesOf({ ...aDataSetForm(), examples: aForm().examples }))
      .toEqual(['With a data set the examples are in the zip; remove the typed examples, or choose "Generate the data for me".']);
    expect(messagesOf({ ...aForm(), dataSet: aDataSetForm().dataSet }))
      .toEqual(['A data set goes only with "I have a data set"; remove it, or choose that option.']);
    expect(messagesOf({ ...aDataSetForm(), dataSet: null })).toEqual(['Choose the zip of your data set and let the page check it.']);
  });

  it('refuses a data set below the minimums, too large, or uploaded elsewhere', () => {
    const set = aDataSetForm().dataSet!;
    expect(messagesOf({ ...aDataSetForm(), dataSet: { ...set, rows: 299, counts: set.counts.map((c, i) => ({ ...c, count: i === 0 ? 79 : 110 })) } }))
      .toEqual(['The data set has 299 rows; it needs at least 300.']);
    expect(messagesOf({ ...aDataSetForm(), dataSet: { ...set, rows: 330, counts: [{ key: 'billing', count: 19 }, { key: 'tech', count: 150 }, { key: 'none', count: 161 }] } }))
      .toEqual(['The option "billing" has 19 rows in the data set; every option needs at least 20.']);
    expect(messagesOf({ ...aDataSetForm(), dataSet: { ...set, bytes: LIMITS.dataSetBytesMax + 1 } })).toEqual(['The data set is 500.0 MB; it may be at most 500.0 MB.']);
    expect(messagesOf({ ...aDataSetForm(), dataSet: { ...set, pathname: 'elsewhere/data.zip' } })).toEqual(['The data set must be a zip uploaded from this page; upload it again.']);
    expect(messagesOf({ ...aDataSetForm(), dataSet: { ...set, counts: set.counts.slice(1) } }))
      .toEqual(['The rows per option must give a whole number for each option, in order: billing, tech, none.']);
    expect(messagesOf({ ...aDataSetForm(), dataSet: { ...set, rows: 400 } })).toEqual(['The rows per option add up to 330, but the data set has 400 rows.']);
  });
});
