import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
    globals: true,
    setupFiles: ["aws-cdk-lib/testhelpers/jest-autoclean"],
    // CDK stack synth in beforeAll can take 20-30s on cold starts
    hookTimeout: 60_000,
    testTimeout: 30_000,
  },
});
