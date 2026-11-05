// A confirmation dialog in the site's own style, in place of the browser's own confirm() box: that box does not
// match the site's retro look and shows the site's address as its title, not what is being asked.
// Modelled on the accessible dialog docs/BenchmarkTerm.tsx opens: a portal mounted only while open (so it is never
// in the prerendered HTML and nothing in the page's layout needs to make room for it), focus moves into the dialog
// when it opens, Tab stays inside it (there are only the two buttons here), and Escape, Cancel or a click outside
// closes it without confirming, giving focus back to whatever was focused before it opened. No inline script: the
// page's content security policy allows none, so this is all React.
import { useEffect, useId, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SECONDARY } from './styles';

export function ConfirmDialog({ open, title, message, confirmLabel, danger = false, onConfirm, onCancel }: {
  open: boolean;
  title: string;
  message: string;
  // The name of the action being confirmed (e.g. "Revoke key"), said on the confirm button.
  confirmLabel: string;
  // A destructive action (one that cannot be undone) gets the site's danger style on the confirm button.
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const id = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    opener.current = document.activeElement;
    cancelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
    // Re-run only when the dialog opens or closes: onCancel is re-created each render, and re-running on every
    // render of an open dialog would move focus back to Cancel on every keystroke elsewhere in the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  // Cancel and the confirm button are the dialog's only controls, so Tab (either way) moves between just the two.
  const keepFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') return;
    event.preventDefault();
    (document.activeElement === cancelRef.current ? confirmRef.current : cancelRef.current)?.focus();
  };

  return createPortal(
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-ink/40 p-4"
      onClick={(event) => { if (event.target === event.currentTarget) onCancel(); }}
    >
      <div
        id={`${id}-dialog`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-body`}
        onKeyDown={keepFocus}
        className="max-h-full w-full max-w-md overflow-y-auto border-2 border-ink bg-panel p-5 text-left shadow-block"
      >
        <p id={`${id}-title`} className="cursor-mark font-pixel text-pixel-20 text-text">{title}</p>
        <p id={`${id}-body`} className="mt-3 text-sm leading-6 text-soft">{message}</p>
        <div className="mt-5 flex flex-wrap gap-3">
          <button ref={cancelRef} type="button" className={`${BUTTON_SECONDARY} px-3 py-1`} onClick={onCancel}>Cancel</button>
          <button ref={confirmRef} type="button" className={`${danger ? BUTTON_DANGER : BUTTON_PRIMARY} px-3 py-1`} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
