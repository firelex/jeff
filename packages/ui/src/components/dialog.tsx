import { useEffect, useRef, type ReactNode } from 'react';

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}

/** A native `<dialog>`, modal while `open`. */
export function Dialog({ open, onClose, title, children, className = '' }: DialogProps) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) throw new Error('Dialog element is missing');
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onCancel={onClose}
      className={['rounded-card border border-line bg-panel p-0 text-text shadow-modal backdrop:bg-black/45', className].join(' ')}
    >
      {title && (
        <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold text-text">{title}</h2>
          <button type="button" aria-label="Close" onClick={onClose} className="text-mute hover:text-text">
            ×
          </button>
        </div>
      )}
      <div className="p-5">{children}</div>
    </dialog>
  );
}
