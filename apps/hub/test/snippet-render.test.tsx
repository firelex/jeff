import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import { Markdown } from '../app/components/docs/Markdown';
import { CODE_LANGUAGE_STORAGE_KEY } from '../app/lib/code-language';
import { CODE_MODE_LABELS, CODE_MODE_STORAGE_KEY } from '../app/lib/code-mode';
import { SNIPPET_FENCE, type SnippetBlock } from '../app/lib/snippet-block';

const both: SnippetBlock = {
  id: 'sdk/functions-1',
  code: [{ language: 'python', code: 'Jeff.remote # python' }, { language: 'rust', code: 'Jeff::remote // rust' }],
  local: [{ language: 'python', code: 'Jeff.local # python' }, { language: 'rust', code: 'Jeff::local // rust' }],
};
const apiOnly: SnippetBlock = { ...both, id: 'sdk/connect-1', local: null };
const localOnly: SnippetBlock = { ...both, id: 'sdk/connect-4', code: null };

const page = (...blocks: SnippetBlock[]) => blocks.map((b) => `\`\`\`${SNIPPET_FENCE}\n${JSON.stringify(b)}\n\`\`\``).join('\n\n');
const show = (...blocks: SnippetBlock[]) => render(<MemoryRouter><Markdown body={page(...blocks)} /></MemoryRouter>);

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('a snippet group', () => {
  it('shows a tab per language, Python first, and the API code with both choices, local first', () => {
    show(both);
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Python', 'Rust']);
    expect(screen.getAllByRole('button').filter((b) => b.hasAttribute('aria-pressed')).map((b) => b.textContent))
      .toEqual([CODE_MODE_LABELS.local, CODE_MODE_LABELS.api]);
    expect(screen.getByText('Jeff.remote # python')).toBeTruthy();
  });

  it('switches language and mode for every block on the page, and remembers both', () => {
    show(both, apiOnly);
    fireEvent.click(screen.getAllByRole('tab', { name: 'Rust' })[0]);
    expect(screen.getAllByText('Jeff::remote // rust')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: CODE_MODE_LABELS.local }));
    expect(screen.getByText('Jeff::local // rust')).toBeTruthy();
    expect(screen.getByText(/Uses the Jeff engine running on your machine, or starts it/)).toBeTruthy();
    expect(window.localStorage.getItem(CODE_LANGUAGE_STORAGE_KEY)).toBe('rust');
    expect(window.localStorage.getItem(CODE_MODE_STORAGE_KEY)).toBe('local');
  });

  it('offers no choice for a group with one form, and shows a local-only group\'s note', () => {
    show(localOnly);
    expect(screen.queryByRole('button', { name: CODE_MODE_LABELS.api })).toBeNull();
    expect(screen.getByText('Jeff.local # python')).toBeTruthy();
    expect(screen.getByText(/Runs the models in this process/)).toBeTruthy();
  });
});
