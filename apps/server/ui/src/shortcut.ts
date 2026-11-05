/** The run shortcut as this platform writes it: Cmd+Enter on Apple systems, Ctrl+Enter elsewhere (both work). */
export function runShortcut(platform: string): string {
  return /Mac|iPhone|iPad|iPod/.test(platform) ? '⌘ Enter' : 'Ctrl Enter';
}

export const RUN_SHORTCUT = runShortcut(navigator.platform);
