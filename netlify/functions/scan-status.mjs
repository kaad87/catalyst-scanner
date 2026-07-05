// GET /api/status — when did the scan workflow last run, and how did it go?
// Lets the dashboard distinguish "no new catalysts" from "not updating".
const REPO = "kaad87/catalyst-scanner";

export default async () => {
  const token = process.env.GITHUB_TOKEN;
  if (!token)
    return Response.json({ error: "GITHUB_TOKEN mangler" }, { status: 501 });

  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/scan.yml/runs?per_page=1`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "catalyst-tape",
      },
    });
  if (!res.ok)
    return Response.json({ error: `GitHub svarede ${res.status}` }, { status: 502 });

  const run = (await res.json()).workflow_runs?.[0];
  if (!run)
    return Response.json({ error: "ingen kørsler" }, { status: 404 });
  return Response.json(
    {
      last_run: run.run_started_at || run.created_at,
      status: run.status,          // queued | in_progress | completed
      conclusion: run.conclusion,  // success | failure | ... (null while running)
    },
    { headers: { "Cache-Control": "public, max-age=30" } });
};

export const config = { path: "/api/status" };
