import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { markdownComponents } from './Markdown';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('markdownComponents', () => {
  it('a copy the clipboard refuses says so with the browser\'s reason, rather than failing silently', async () => {
    const writeText = vi.fn(async () => {
      throw new DOMException('Write permission denied.', 'NotAllowedError');
    });
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    render(<ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{'```bash\necho hi\n```'}</ReactMarkdown>);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(await screen.findByText('Copy failed: Write permission denied.')).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith('echo hi');
  });

  it('does not render a raw <script> tag in the Markdown as an HTML element', () => {
    const markdown = 'Some text.\n\n<script>window.__xss__ = true;</script>\n\nMore text.';
    const { container } = render(<ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{markdown}</ReactMarkdown>);
    expect(container.querySelector('script')).toBeNull();
    expect((window as unknown as { __xss__?: boolean }).__xss__).toBeUndefined();
  });

  it('opens an external link with rel="noopener noreferrer", and a same-page link with no target', () => {
    const markdown = '[External](https://example.com/page) and [Internal](#playground).';
    render(<ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{markdown}</ReactMarkdown>);
    const external = screen.getByText('External');
    expect(external.getAttribute('rel')).toBe('noopener noreferrer');
    expect(external.getAttribute('target')).toBe('_blank');
    const internal = screen.getByText('Internal');
    expect(internal.getAttribute('target')).toBeNull();
    expect(internal.getAttribute('rel')).toBeNull();
  });
});
