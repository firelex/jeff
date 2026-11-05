import { defineConfig } from 'drizzle-kit';

// `pnpm db:generate` writes a new migration into drizzle/ from app/lib/schema.ts (it needs no database);
// `pnpm db:migrate` applies them to DATABASE_URL_UNPOOLED, Neon's direct connection (a migration should not go
// through the pooler), read from .env.migrate (written by `vercel env pull`; the owner's
// accounts runbook). Migrations are run by hand, never in the Vercel build.
const url = process.env.DATABASE_URL_UNPOOLED;
export default defineConfig({
  dialect: 'postgresql',
  schema: './app/lib/schema.ts',
  out: './drizzle',
  ...(url === undefined ? {} : { dbCredentials: { url } }),
});
