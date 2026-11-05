import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["test/**/*.test.ts"], exclude: ["test/**/*.slow.test.ts"], testTimeout: 30000 },
});
