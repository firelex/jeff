// The engine socket every local code sample attaches to (content/home-example-local.<ext>, and each worked example's
// "On your machine" code): the reader starts a Jeff engine listening there with engineServe. No runtime code from a
// .server file, so the browser bundle can import it.
export const ENGINE_SOCKET = '/tmp/jeff.sock';

// The command that starts the engine serving `adapter` (its id) on ENGINE_SOCKET, with the batching options the
// engine requires, as the home page and scripts/hub_examples.py --check-local start it.
export function engineServe(adapter: string): string {
  return `jeff runtime serve --socket ${ENGINE_SOCKET} --adapter ${adapter} --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2`;
}
