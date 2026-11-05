import { fileURLToPath } from 'node:url';
import path from 'node:path';

// apps/hub/app/lib -> the repository root.
export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
export const HUB_ROOT = path.join(REPO_ROOT, 'apps/hub');
