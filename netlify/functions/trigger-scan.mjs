// POST /api/scan — triggers the GitHub Actions scan workflow.
// Requires a fine-grained PAT (Actions: read+write on this repo) in the
// Netlify env var GITHUB_TOKEN.
const REPO = "kaad87/catalyst-scanner";

export default async (req) => {
  if (req.method !== "POST")
    return new Response("POST only", { status: 405 });

  const token = process.env.GITHUB_TOKEN;
  if (!token)
    return Response.json(
      { error: "GITHUB_TOKEN mangler i Netlify env — se README" },
      { status: 501 });

  const gh = (path, init = {}) =>
    fetch(`https://api.github.com/repos/${REPO}${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "catalyst-tape",
        ...init.headers,
      },
    });

  const runs = await gh("/actions/runs?status=in_progress&per_page=1")
    .then(r => r.json()).catch(() => ({}));
  if (runs.total_count > 0)
    return Response.json({ status: "already_running" }, { status: 202 });

  const res = await gh("/actions/workflows/scan.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({ ref: "main" }),
  });
  if (res.status === 204)
    return Response.json({ status: "triggered" }, { status: 202 });
  return Response.json({ error: `GitHub svarede ${res.status}` }, { status: 502 });
};

export const config = { path: "/api/scan" };
