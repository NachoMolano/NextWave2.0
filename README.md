# Volta

A voice agent that runs the drayage leg of a shipment — the truck from the port to the
warehouse — entirely by phone. It calls carriers, negotiates rate and pickup window inside a
mandate a human set beforehand, and turns spoken conversation into commitments you can audit
afterwards.

Built for the NextWave hackathon (Yuno + Nauta). The challenge is in
[`docs/CHALLENGE.md`](docs/CHALLENGE.md).

## The idea

**Speech is probabilistic. Authority is deterministic.**

The model talks. It cannot decide. Every rate a carrier says on the phone arrives at a
deterministic policy engine — plain Python, no model in the loop — which checks it against
the mandate and writes down the verdict, including the refusals. "Your boss already approved
ten thousand five hundred" is information about what someone wants, never authorization to
do it.

That split is the product, and it is enforced structurally rather than by discipline:

- `app/policy/` may import only `app/domain/`. It therefore cannot call a model, cannot
  reach the network, and cannot read a prompt. `tests/test_layering.py` checks this on every
  run.
- The database refuses a second award (`unique (order_id) where status = 'accepted'`), a
  commitment with no audio anchor (`evidence_anchor_ms not null`), and a replayed webhook
  (`unique (idempotency_key)`).
- A call can only ever produce a *pre-agreement*. What creates a commitment is the written
  recap actually being delivered.

## The three flows

1. **Quote and book.** A container lands, a human sets a ceiling and a window, and the agent
   calls three carriers in parallel, compares the quotes, and hands a human the ranked
   comparison — losers and reasons included — before anything is booked.
2. **Someone calls us.** A dispatcher rings about a delay. The agent verifies who they are
   against the order before revealing anything, classifies the call, and elevates a report.
3. **The deadline passed.** Nothing was delivered and nobody called. The agent calls to ask
   what happened and escalates the answer.

## Stack

Supabase for state and evidence · Vapi for the voice agent and telephony (a Twilio number
imported into it) · FastAPI + Python 3.12 behind them.

## Running it

```bash
cd backend
uv sync
cp .env.example .env      # fill it in
uv run pytest             # no network, no database, no phone call
uv run uvicorn app.main:app --reload --port 8000
```

## Where to look

| | |
| --- | --- |
| [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) | folder structure, schema, workflow, and who builds what |
| [`AGENTS.md`](AGENTS.md) | the invariants and the working rules |
| [`backend/tests/test_layering.py`](backend/tests/test_layering.py) | the architecture, as a test |
| [`supabase/migrations/0001_init.sql`](supabase/migrations/0001_init.sql) | the schema, with the reasoning in the comments |
