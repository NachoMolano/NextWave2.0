# frontend

The portal. User-operated, never reachable from a phone call.

Vite + React + TypeScript, no router and no state library. Ported from the control tower in
the old repo: the design system, the shell and the page shapes carried over; the data layer
was rewritten, because the vocabulary changed underneath it.

```bash
npm install
npm run dev        # http://localhost:5173, proxying /api to the backend
npm run build      # tsc -b && vite build
npm run lint
```

Point the proxy at a running backend with `VITE_API_PROXY_TARGET` (see `.env.example`);
it defaults to `http://127.0.0.1:8000`.

## What it talks to

`/api` only, through `src/api.ts`. Two rules that are not style preferences:

**There is no Supabase client in this app and there must not be one.** The service key is
server-side. What a person may see of a call recording or a refusal is a redaction decision
made by code that policy has already seen — not a row filter running in a browser.

**It never calls `/vapi`.** That is the surface an unauthenticated stranger on a phone
reaches. The backend keeps the two in separate packages for that reason, and so does this.

In development Vite proxies `/api`, so the browser makes a same-origin request and no CORS
policy has to exist. A deployed build either ships from the same origin as the API, or sets
`VITE_API_BASE_URL` and gets a narrow CORS allowlist on the backend — `*` would be the wrong
answer for a surface carrying the only endpoint that can write a price cap.

## The screens

| | |
|---|---|
| Operations | The queue. The demurrage countdown is the first thing on the row because it is the thing that makes everything else urgent. |
| Operation | Mandate, market, quotes, calls, commitment — the whole aggregate in one request, so a human approving an award is not watching a page fill in. |
| Approvals | One inbox. Award decisions, escalations and incidents are the same request from here: somebody has to decide. |
| Carriers | Who Volta may call. Being on file is decided here, never on the phone. |
| Call evidence | Transcript with audio offsets, the model's brief, the recording. |

## Four things the UI is careful about

These are the reason the copy reads the way it does. They are not decoration.

- **No mandate is not "no limit."** An order without one says *nothing is authorized*, and
  the button that opens the market is disabled. A ceiling is a permission somebody granted.
- **Granting a mandate requires a name.** The submit button stays disabled until you type
  one, because that field is the row a jury reads when it asks who authorized the spend.
  Same for deciding an approval.
- **Superseded quotes stay on screen.** They said 8,500 and then they said 9,200, and both
  were said. A market that shows only the current number has deleted the evidence.
- **`verbal` and `recap_sent` say "not booked."** A commitment is only a booking once the
  written recap is confirmed delivered. Anything softer would be the screen making a promise
  the system has not made.
