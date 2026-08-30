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
