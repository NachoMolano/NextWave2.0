# Deploying the portal

## Vercel

| Setting | Value |
|---|---|
| Application Preset | **Vite** |
| Root Directory | **`frontend`** |
| Build command | `npm run build` (the preset default) |
| Output directory | `dist` (the preset default) |
| Environment variables | **none** |

No environment variables, and that is the point of `vercel.json`. It rewrites `/api/*` to the
backend, so the browser makes a **same-origin** request to the Vercel domain and Vercel proxies
it onward. Nothing is cross-origin, so no CORS policy has to exist — which matters on a surface
that carries the only endpoint able to write a price cap. `*` would be the wrong answer there,
and under demo pressure `*` is what people reach for.

The destination is `https://volta-backend-778k.onrender.com`, hardcoded in `vercel.json`.
Vercel cannot interpolate an environment variable into a rewrite destination, so this is
committed config rather than a dashboard setting — which is arguably better: it is reviewable
in a diff. **If the backend moves, change it here and redeploy.**

The third rewrite sends everything else to `index.html`. The portal routes on the hash, so it
does not strictly need it today, but a deep link would 404 without it the moment routing changes.

`VITE_API_BASE_URL` still exists as an escape hatch in `src/api.ts` for a deployment where the
proxy is not wanted. If you set it, the browser talks to the backend cross-origin and the
backend then **does** need a CORS allowlist. Prefer the rewrite.

## The backend is not a Vercel app

It cannot go here, and the reason is not packaging:

- `main.py` starts a background loop that sweeps for missed delivery deadlines every 60
  seconds. Serverless functions do not have a process that stays alive between requests, so
  OUTBOUND 2 simply would not happen.
- Vapi posts webhooks to a fixed `server.url` and expects an answer to `assistant-request`
  within a hard, non-configurable **7.5 seconds**. A cold start eats that budget, and a missed
  budget means the caller gets the unverified-caller assistant instead of the right one.
- It holds the Supabase service key, which bypasses RLS. That belongs on a server you control,
  not in an edge runtime replicated wherever.

It is deployed on **Render**, described by `render.yaml` at the repository root and built
by `backend/Dockerfile`. Render prompts for every secret (`sync: false`), so none of them live
in the repo. The variables it needs:

```
PORTAL_TOKENS                              who may use the portal, and as whom
SUPABASE_URL, SUPABASE_SECRET_KEY          the database
VAPI_API_KEY, VAPI_PHONE_NUMBER_ID         placing calls
VAPI_SERVER_SECRET                         verified before any webhook body is parsed
VAPI_MODEL, VAPI_VOICE_ID, VAPI_TRANSCRIBER
OPENAI_API_KEY, OPENAI_REPORT_MODEL        post-call extraction only
RESEND_API_KEY, NOTIFY_FROM_EMAIL          the written recap
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM
MANAGER_EMAIL, MANAGER_WHATSAPP, ESCALATION_PHONE_NUMBER
PUBLIC_BASE_URL                            what Vapi calls back into
ENVIRONMENT=demo
```

Then point the Vapi phone number's server URL at `https://<backend>/vapi/events`, and put
`https://<backend>` into `vercel.json`.

`healthCheckPath` is `/health`, which answers without touching the network on purpose: it is
the endpoint you need most when the database is the thing that is broken, so it stays a truthful
liveness signal rather than a database check wearing a health check's name.


## The portal's credentials

Every `/api` route needs `Authorization: Bearer <token>`. The operator types the token once on
the sign-in screen and it is kept in `sessionStorage` — never in the built bundle, because Vite
inlines `import.meta.env` values into public JavaScript and a token shipped that way is readable
by anyone who opens devtools.

**Set `PORTAL_TOKENS`, one token per person:**

```
PORTAL_TOKENS=<long-random>:maria@volta.mx,<long-random>:diego@volta.mx
```

Generate each with `openssl rand -base64 32`.

This matters more than it looks. The identity is taken from the credential — never from the
request body — and written into `mandate_set_by` and `decided_by`. Those are the rows somebody
reads when they ask who authorized the spend. With a single shared token, "maria@volta.mx
approved this" actually means "somebody holding the shared token approved this": a human-looking
name claiming more accountability than the system can back. One token per person makes the same
row true, and lets one person's access be revoked without rotating everyone's.

`PORTAL_API_TOKEN` + `PORTAL_MANAGER_IDENTITY` remain as a single-token fallback for a demo. The
portal says which mode it is in — the sidebar reads *acting as X*, and admits when that name is
the deployment's rather than a person's.

With none of them set the portal answers **503**, not 200. `/api` carries the only endpoint that
can write a price cap; failing open there would be the wrong default.
