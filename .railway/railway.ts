import { defineRailway, project, service } from "railway/iac";

// finance-api: the finance suite entry point (SPEC-001). Stateless: no database, no volume.
// Variables stay unmanaged (they live in Railway only). It sleeps when idle (Railway Serverless).
// Runs in Singapore next to the finance cells; the IaC does not carry the region, so after the
// first deploy: railway scale --service finance-api asia-southeast1-eqsg3a=1 us-east4-eqdc4a=0
// Deploys from GitHub main after CI passes (Railway Wait for CI). No deploy job in CI, so the two
// never wait on each other.
const github = {
  type: "github" as const,
  repo: "bookchaowalit/finance-api",
  branch: "main",
  checkSuites: true,
};

export default defineRailway(() => {
  const api = service("finance-api", {
    source: github,
    healthcheck: "/health",
    healthcheckTimeout: 60,
    deploy: { sleepApplication: true },
  });

  return project("finance-api", {
    variables: { managed: false },
    resources: [api],
  });
});
