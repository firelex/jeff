import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import { DocsLayout } from '../app/components/docs/DocsLayout';
import { Markdown } from '../app/components/docs/Markdown';

const sidebar = { guides: [{ group: 'Start', pages: [{ id: 'a', title: 'Page A' }, { id: 'b', title: 'Page B' }] }], sdk: [] };

function renderLayout() {
  return render(
    <MemoryRouter>
      <DocsLayout tab="guides" sidebar={sidebar} current="b" headings={[{ id: 'section-one', text: 'One', depth: 2 }]}>
        <p>body</p>
      </DocsLayout>
    </MemoryRouter>,
  );
}

describe('DocsLayout', () => {
  it('marks the current page in the sidebar', () => {
    renderLayout();
    expect(screen.getAllByRole('link', { name: 'Page B' })[0].getAttribute('aria-current')).toBe('page');
  });
  it('lists the page headings under On this page', () => {
    renderLayout();
    expect(screen.getByRole('link', { name: 'One' }).getAttribute('href')).toBe('#section-one');
  });
  it('opens the contents menu on small screens', () => {
    renderLayout();
    fireEvent.click(screen.getByRole('button', { name: 'Contents' }));
    expect(screen.getByRole('dialog', { name: 'Contents' })).toBeTruthy();
  });
  it('shows the three tabs', () => {
    renderLayout();
    for (const name of ['Guides', 'SDK', 'API reference']) expect(screen.getByRole('link', { name })).toBeTruthy();
  });
});

describe('Markdown', () => {
  it('gives headings the ids the loader computed', () => {
    const { container } = render(<MemoryRouter><Markdown body={'## Functions and /v1/decide'} /></MemoryRouter>);
    expect(container.querySelector('h2')?.id).toBe('section-functions-and-v1-decide');
  });
  it('gives h3 headings ids too, since the loader indexes depth 3 as well', () => {
    const { container } = render(<MemoryRouter><Markdown body={'### A sub heading'} /></MemoryRouter>);
    expect(container.querySelector('h3')?.id).toBe('section-a-sub-heading');
  });
  it('leads section headings with the block cursor, in the pixel font', () => {
    const { container } = render(<MemoryRouter><Markdown body={'## A section'} /></MemoryRouter>);
    const h2 = container.querySelector('h2')!;
    expect(h2.className).toContain('cursor-mark');
    expect(h2.className).toContain('font-pixel');
  });
  it('shows code on a dark screen inside a frame whose title bar names the language', () => {
    const { container } = render(<MemoryRouter><Markdown body={'```python\nx = 1\n```'} /></MemoryRouter>);
    expect(container.querySelector('pre')!.className).toContain('bg-screen');
    expect(screen.getByText('python').className).toContain('cursor-mark');
  });
  it('renders code with a copy button and raw HTML as text', () => {
    render(<MemoryRouter><Markdown body={'```python\nx = 1\n```\n\n<b>hi</b>'} /></MemoryRouter>);
    expect(screen.getByRole('button', { name: /copy/i })).toBeTruthy();
    expect(screen.queryByText('hi', { selector: 'b' })).toBeNull();
  });
});
