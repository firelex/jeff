// The hub endpoints' settings: environment variables read by name. A setting that is not set, or is empty, is an
// error naming it; nothing has a default.

export class MissingSettingsError extends Error {
  readonly missing: string[];

  // `reason` says why the settings are needed, when that depends on another setting.
  constructor(missing: string[], reason?: string) {
    super(`${missing.length === 1 ? 'the setting' : 'the settings'} ${missing.join(', ')} ${missing.length === 1 ? 'is' : 'are'} not set (or empty)${reason === undefined ? '' : `: ${reason}`}`);
    this.name = 'MissingSettingsError';
    this.missing = missing;
  }
}

// The value of every setting in `names`, or a MissingSettingsError naming every one that is missing (all of them at
// once, so one look at the error is enough to fix the deployment).
export function readSettings<const Name extends string>(names: readonly Name[], env: NodeJS.ProcessEnv = process.env): Record<Name, string> {
  const missing = names.filter((name) => {
    const value = env[name];
    return value === undefined || value.trim() === '';
  });
  if (missing.length > 0) throw new MissingSettingsError(missing);
  return Object.fromEntries(names.map((name) => [name, env[name] as string])) as Record<Name, string>;
}
