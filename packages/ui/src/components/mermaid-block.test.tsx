import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { MermaidBlock } from './mermaid-block';

const { initialize, renderDiagram } = vi.hoisted(() => ({
  initialize: vi.fn(),
  renderDiagram: vi.fn(),
}));

vi.mock('mermaid', () => ({
  default: { initialize, render: renderDiagram },
}));

afterEach(() => {
  initialize.mockClear();
  renderDiagram.mockReset();
});

// These tests run in declaration order (vitest does not shuffle by default) because mermaid-block.tsx only ever
// calls mermaid.initialize() once, the first time any block mounts anywhere on the page; the first test below is
// that first mount, and later tests check that a later block reuses it instead of initialising again.
describe('MermaidBlock', () => {
  it('lazily initialises mermaid with a strict security level and renders the svg mermaid.render returns', async () => {
    renderDiagram.mockResolvedValue({ svg: '<svg role="img" aria-label="diagram"></svg>' });
    const { container } = render(<MermaidBlock code="flowchart LR; a --> b" />);
    await waitFor(() => expect(container.querySelector('svg')).not.toBeNull());
    expect(initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: 'strict' }));
    expect(renderDiagram).toHaveBeenCalledWith(expect.any(String), 'flowchart LR; a --> b');
  });

  it('shows the error and the raw diagram source, not a blank block, when mermaid.render rejects', async () => {
    renderDiagram.mockRejectedValue(new Error('Parse error on line 1'));
    const { findByText, getByText } = render(<MermaidBlock code="not a diagram" />);
    expect(await findByText(/Parse error on line 1/)).toBeTruthy();
    expect(getByText('not a diagram')).toBeTruthy();
  });

  it('does not re-initialise mermaid for a later block, and gives two diagrams on one page different render ids', async () => {
    renderDiagram.mockResolvedValue({ svg: '<svg></svg>' });
    render(
      <>
        <MermaidBlock code="a" />
        <MermaidBlock code="b" />
      </>,
    );
    await waitFor(() => expect(renderDiagram).toHaveBeenCalledTimes(2));
    expect(initialize).not.toHaveBeenCalled();
    const ids = renderDiagram.mock.calls.map((call) => call[0]);
    expect(new Set(ids).size).toBe(2);
  });
});
