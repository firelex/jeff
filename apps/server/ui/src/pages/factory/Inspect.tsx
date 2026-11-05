import { useState } from 'react';
import { Download, FileText } from 'lucide-react';
import { Button, Dialog } from '@jeff/ui';
import { shownError, type ShownError } from '../../api';
import { ErrorAlert } from '../../components/ErrorAlert';
import type { FactoryApi } from './api';

const LABELS: Record<string, string> = {
  'questions.json': 'The questions, verbatim', 'conditions.json': 'When each question is read',
  'signatures.json': 'Function arguments, values and defaults', 'description.md': 'Task description for the teacher',
  'README.md': 'How these files were made', 'source/spec.json': 'Cookbook spec.json', 'source/trader.py': 'Cookbook trader.py',
  'source/dispatch.py': 'Cookbook dispatch.py', 'source/SOURCE.md': 'Where they come from', 'source/LICENSE': 'Cookbook licence (MIT)',
};

// The files behind an imported question set, to open and download: the originals and what was derived from them.
// A file with no description above is shown under its own path.
export function Inspect({ factory, reference, files }: { factory: FactoryApi; reference: string; files: string[] }) {
  const [open, setOpen] = useState<{ file: string; text: string } | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  const show = async (file: string) => {
    setError(null);
    try {
      setOpen({ file, text: await factory.referenceFile(reference, file) });
    } catch (failure) {
      setError(shownError(failure));
    }
  };
  const save = () => {
    if (!open) throw new Error('No file is open to download');
    const url = URL.createObjectURL(new Blob([open.text], { type: 'text/plain' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = open.file.split('/').pop() as string;
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="flex flex-col gap-2">
      <p className="m-0 text-xs font-medium text-text">Inspect the files</p>
      <div className="flex flex-wrap gap-2">
        {files.map((file) => (
          <Button key={file} variant="ghost" size="sm" onClick={() => void show(file)} title={file}>
            <FileText className="size-4" aria-hidden="true" />{LABELS[file] ?? file}
          </Button>
        ))}
      </div>
      {error && <ErrorAlert error={error} />}
      <Dialog open={open !== null} onClose={() => setOpen(null)} className="w-[min(56rem,92vw)]"
        title={open && <span className="font-mono">{open.file}</span>}>
        {open && (
          <div className="flex flex-col gap-3">
            <p className="m-0 text-xs text-mute">{LABELS[open.file] ? `${LABELS[open.file]} · ` : ''}{reference}</p>
            <pre className="m-0 max-h-[60vh] overflow-auto rounded-button border border-line bg-background p-4 font-mono text-xs leading-relaxed">{open.text}</pre>
            <div className="flex justify-end">
              <Button variant="ghost" onClick={save}><Download className="size-4" aria-hidden="true" />Download</Button>
            </div>
          </div>
        )}
      </Dialog>
    </div>
  );
}
