# Evaluation — how the jury scores this

Source: **Evaluation guidelines · NextWave Hackathon 2026**, Module 06
(`https://nextwave-hackathon-2026.vercel.app/judging`), also in this repo as a PDF.
Everything in §1–§5 is the jury's own wording, condensed. §6 is ours and is marked as such.

**Pitches are Sunday 30 August, immediately after code freeze. Ten minutes per team:
short pitch → live demo → trial by fire → technical Q&A.** City champions then give a
15-minute final (10 present + 5 questions). Judges come from Yuno and Nauta; every project
is seen by the full panel, who rank independently and then deliberate.

---

## 1. Three principles

> **Depth over difficulty.** Picking the hardest challenge earns nothing by itself. A modest
> scope solved deeply beats an ambitious scope solved superficially.

> **Working beats promised.** We evaluate what runs in front of us, live, not what the
> slides say it will do.

> **Judgment beats spectacle.** The technical defense weighs as much as the demo. A
> spectacular demo the team can't explain loses to a simpler demo defended with clear
> reasoning.

## 2. The five lenses

Applied to every project, roughly in order of weight. None alone decides a winner.

| # | Lens | The question the jury asks |
| --- | --- | --- |
| 1 | **Does it work?** | Does the system run end to end and pass the trial by fire — reacting correctly to what the judges change live, **without the team touching anything**? |
| 2 | **Depth & judgment** | Is the architecture sound? Can the team explain every major decision, the alternatives they rejected and why? Does the decision log show real trade-offs? |
| 3 | **Solves the real problem** | Does it hit the challenge's objective *as written* — including the ugly cases — rather than a generic product that happens to be nearby? |
| 4 | **Originality** | Is there an idea here we haven't seen before — an approach, an insight, a mechanism — or is it the obvious solution executed adequately? |
| 5 | **Experience & clarity** | Would the human on the other side actually use it? Is the pitch clear, the demo legible, **the repo readable by someone who wasn't there**? |

## 3. Deliverables

> Slides, demo, public GitHub repo with **README**, **architecture diagram**, **decision log**.
> **Missing deliverables are noticed.**

## 4. What does not score

- Number of features, slides, integrations, or lines of code.
- Buzzwords. **Naming a framework isn't a design decision — knowing why you chose it is.**
- A polished video of something that doesn't run live.
- **Building for the rubric.** Teams that chase these five lenses one by one usually end up
  with a shallow project on all five.

## 5. The jury's one piece of advice

> Get the thinnest possible version working end to end in the first hours. Then spend the
> rest of the 24h making it deep — handling the ugly cases, understanding your own
> trade-offs, and rehearsing the trial by fire. Teams that do this in the other order run
> out of time with a beautiful front and nothing behind it.

---

## 6. What this means for how we work — *ours, not the jury's*

Read §4 first: the last bullet warns against exactly the kind of checklist this section
could become. This is not a scoring plan. It exists so that an agent picking up a task
knows which artifacts are load-bearing and does not casually delete or hollow one out.

**Lens 1 outranks everything, and it is binary.** "Without the team touching anything"
means a scripted or hand-held demo scores as not working. A feature that is not wired into
the live path contributes nothing, however well built. When in doubt, finish the path
before deepening any part of it.

**Lens 2 is where this repo's existing discipline pays.** The jury asks for rejected
alternatives, which is the format `docs/DECISION_LOG.md` already uses — decided / beat /
why / would change if. Keep that shape. A decision recorded without its rejected
alternative is worth much less than one that names what it beat.

**Lens 3 is `docs/UGLY_CASES.md`.** The brief's ugly cases are the objective *as written*,
which is why that table is the test suite and not documentation. A handled case with no row
and no test is invisible to the jury.

**Lens 4 rewards the mechanism, not the stack.** The defensible answer to "what is original
here" is not a vendor list; it is that authorization is deterministic and enforced
structurally — the import graph in `tests/test_layering.py` and the constraints in
`supabase/migrations/`, both of which fail the build when violated.

**Lens 5 includes the repo.** "Readable by someone who wasn't there" is a judged property
of `README.md`, `AGENTS.md` and `docs/`, not just of the pitch.

### Known gaps against §3, as of 2026-08-29

Stated plainly so nobody assumes they are covered:

- **No architecture diagram exists in the repo.** `docs/ARCHITECTURE.md` has an ASCII
  dependency graph and `docs/schema.dbml` renders the database in dbdiagram, but there is
  no committed diagram file. The jury names this as a deliverable and says missing ones are
  noticed.
- **`main` and `martin` have diverged.** Neither contains the other. The public repo shows
  `main` by default, so whatever a judge reads is whatever is on `main`.
