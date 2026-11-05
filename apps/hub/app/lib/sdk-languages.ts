// The SDK languages the site can show code in, in the order their tabs appear (before an example's curl tab). Which
// of them a build shows is content/sdk-languages.yaml (sdk-languages.server.ts reads it); a language that is off is
// not generated at all, so its code never reaches a built page. No imports: the browser bundle and
// scripts/check-build.mjs (plain Node, type stripping) both load this file.
export const SDK_LANGUAGES = ['python', 'node', 'rust', 'csharp', 'java'] as const;
export type SdkLanguage = (typeof SDK_LANGUAGES)[number];

// The tab labels. They are set in Jeff Pixel, so each must use only characters the font draws ('#' is one;
// scripts/check-build.mjs checks every pixel-font character of the built pages).
export const SDK_LABELS: Record<SdkLanguage, string> = {
  python: 'Python',
  node: 'Node',
  rust: 'Rust',
  csharp: 'C#',
  java: 'Java',
};

// The name a code block's title bar gives the code (CodeBlock's `language`).
export const SDK_CODE_NAMES: Record<SdkLanguage, string> = {
  python: 'python',
  node: 'javascript',
  rust: 'rust',
  csharp: 'csharp',
  java: 'java',
};

// Text every snippet of a language contains and nothing else on the site does: its import of the SDK (the local,
// remote and HTTP-transport snippets all start with it). scripts/check-build.mjs fails a build in which a language that is off
// left any of its markers in a built file. Python is always shown; Node's SDK pages show Node code whatever the
// setting, so Node has no marker either.
export const SDK_CODE_MARKERS: Record<'rust' | 'csharp' | 'java', string[]> = {
  rust: ['use jeff_sdk::'],
  csharp: ['using Jeff.Sdk;'],
  java: ['import ai.jeff.sdk.'],
};

export function isSdkLanguage(value: string | null): value is SdkLanguage {
  return (SDK_LANGUAGES as readonly (string | null)[]).includes(value);
}
