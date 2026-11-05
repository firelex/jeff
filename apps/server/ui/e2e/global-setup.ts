// Starts apps/server/tests/console_server.py (a real Jeff server over a fake engine, on its own free port) before
// the smoke tests run, and stops it afterwards. Playwright's `webServer` option needs the URL to poll known in
// advance; this server picks its own free port instead (so two runs, or a run beside another Jeff server on this
// machine, never collide), so a global setup that spawns it and reads the port and the test accounts' credentials
// off its first line of stdout is used instead. The credentials become environment variables; because Playwright
// forks each test file's worker process from this one only after global setup returns, the workers inherit them.
import { spawn, type ChildProcess } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const STARTUP_TIMEOUT_MS = 30_000;
const SHUTDOWN_TIMEOUT_MS = 10_000;

interface Credentials {
  url: string;
  usersFile: string;
  admin: { email: string; password: string };
  member: { email: string; password: string };
}

function isCredentials(value: unknown): value is Credentials {
  return (
    typeof value === 'object' && value !== null &&
    typeof (value as Credentials).url === 'string' && typeof (value as Credentials).usersFile === 'string' &&
    typeof (value as Credentials).admin?.email === 'string' && typeof (value as Credentials).admin?.password === 'string' &&
    typeof (value as Credentials).member?.email === 'string' && typeof (value as Credentials).member?.password === 'string'
  );
}

function startServer(): Promise<{ child: ChildProcess; credentials: Credentials }> {
  return new Promise((resolve, reject) => {
    const child = spawn('uv', ['run', 'python', 'apps/server/tests/console_server.py'], {
      cwd: ROOT,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let buffered = '';
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill('SIGKILL');
      reject(new Error(`console_server.py printed no credentials within ${STARTUP_TIMEOUT_MS}ms`));
    }, STARTUP_TIMEOUT_MS);

    const onStdout = (chunk: Buffer) => {
      buffered += chunk.toString('utf-8');
      const newline = buffered.indexOf('\n');
      if (newline === -1 || settled) return;
      const line = buffered.slice(0, newline);
      let parsed: unknown;
      try {
        parsed = JSON.parse(line);
      } catch (error) {
        settled = true;
        clearTimeout(timer);
        child.kill('SIGKILL');
        reject(new Error(`console_server.py's first stdout line was not JSON (${String(error)}): ${line}`));
        return;
      }
      if (!isCredentials(parsed)) {
        settled = true;
        clearTimeout(timer);
        child.kill('SIGKILL');
        reject(new Error(`console_server.py's credentials line is missing a field: ${line}`));
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve({ child, credentials: parsed });
    };

    child.stdout?.on('data', onStdout);
    child.stderr?.on('data', (chunk: Buffer) => process.stderr.write(chunk));
    child.once('error', (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.once('exit', (code, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error(`console_server.py exited (code ${code}, signal ${signal}) before printing credentials`));
    });
  });
}

async function waitUntilServing(url: string): Promise<void> {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${url}/ui/`);
      if (response.ok) return;
      lastError = new Error(`${url}/ui/ answered ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`${url}/ui/ never answered 200 (last: ${String(lastError)})`);
}

async function stopServer(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.pid === undefined) return;
  child.kill('SIGTERM');
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      if (child.exitCode === null) child.kill('SIGKILL');
    }, SHUTDOWN_TIMEOUT_MS);
    child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  const { child, credentials } = await startServer();
  try {
    await waitUntilServing(credentials.url);
  } catch (error) {
    // Playwright never calls the teardown below when global setup throws, so stop the server here, then fail.
    await stopServer(child);
    throw error;
  }

  process.env.JEFF_CONSOLE_E2E_URL = credentials.url;
  process.env.JEFF_CONSOLE_E2E_USERS_FILE = credentials.usersFile;
  process.env.JEFF_CONSOLE_E2E_ADMIN_EMAIL = credentials.admin.email;
  process.env.JEFF_CONSOLE_E2E_ADMIN_PASSWORD = credentials.admin.password;
  process.env.JEFF_CONSOLE_E2E_MEMBER_EMAIL = credentials.member.email;
  process.env.JEFF_CONSOLE_E2E_MEMBER_PASSWORD = credentials.member.password;

  return () => stopServer(child);
}
