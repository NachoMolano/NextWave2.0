# Decision Log — execute

NextWave Hackathon 2026 · Bogotá

## 1. Challenge decision  `T+01:21`

**Options considered**

- The Buyer Who Isn't Human
- The Control Tower
- The Interface That Builds Itself
- The Agent on the Line

**Chosen:** The Agent on the Line

**Why:** This problem stands at the intersection of multiple aspects of the logistics business, it requires us to build an understanding of multiple actors involved, specific business processes and guardrails for hundreds of conversational scenarios. It is also, one of the least selected problems, giving us the opportunity to stand out as a unique, powerful solution.

## 2. AI Voice Architecture Decision  `T+03:05`

**Options considered**

- OpenAI Realtime
- Cascade (Separate STT, TTS, LLM)

**Chosen:** Cascade (Separate STT, TTS, LLM)

**Why:** While gpt-realtime wins in latency and asyn tool calling, the cascade architecture allows more customization of providers, implementation of security guardrails, and lower costs.

## 3. Transcribe live calls, save timestamped evidence in Supabase, and generate the recap after the call.  `T+03:15`

**Options considered**

- Post-call-only transcription, real-time model-driven commitments, or a mutable transcript without audio references.

**Chosen:** OpenAI transcription in real time with append-only transcript events linked to the Twilio call and audio offset; policy and recap run afterward.

**Why:** It keeps the call responsive and cost-efficient while preserving auditable evidence and preventing unverified speech from creating a commitment.

## 4. Selected DB: Postgres (Supabase) vs MongoDB  `T+14:28`

**Options considered**

- Postgres (Supabase)
- MongoDB

**Chosen:** Postgres (Supabase)

**Why:** Postgres insures that no two trucks are booked for a same container. With MongoDB it must be defined as code logic. Additionally, as a group we have greater experience using Postgres, which makes it easier to handle an E2E project.

## 5. AI Voice Agent Provider  `T+14:39`

**Options considered**

- - Self-built AI Voice Agent (STT, LLM, TTS, VAD, etc)
- - External provider (Vapi)

**Chosen:** Vapi

**Why:** Building our own AI Voice system from scratch gives a lot of personalization, but implies too much work in terms of achieving good latency, guardrails and quality, which external providers already support. We are not focusing on the part of the solution that already exists, but focus on the tools and integrations that only we can build.

## 6. Use Vapi as the managed voice and telephony platform instead of maintaining our own real-time voice stack.  `T+23:14`

**Options considered**

- Maintain our own Twilio streaming, speech-to-text, text-to-speech, interruption, and call-transfer pipeline
- Use Vapi for the complete voice runtime
- Build a hybrid system with Vapi plus custom audio-stream processing

**Chosen:** Use Vapi for calls, transcription, speech generation, turn-taking, recording artifacts, and transfers, while keeping our business tools and authorization logic in our backend.

**Why:** This made the end-to-end telephone workflow feasible within the project timeline and removed substantial real-time audio complexity. The compromise is strong vendor dependence: payload formats, timing, concurrency, model behavior, and artifact availability are controlled by Vapi. Provider changes can break integration behavior, and we have less control over latency, interruption handling, and raw utterance preservation.

## 7. Separate probabilistic conversation from deterministic authorization.  `T+23:14`

**Options considered**

- Allow the language model to negotiate and authorize agreements directly
- Use prompts as the principal security and authorization mechanism
- Let the model propose actions but require deterministic Python policy to authorize them

**Chosen:** The model may interpret speech and propose quotes, incidents, or agreement candidates, but deterministic policy and state-machine code decide whether anything can be awarded or committed.

**Why:** This prevents persuasive callers, prompt injection, or model mistakes from changing the mandate or creating unauthorized commitments. The compromise is reduced conversational flexibility and more engineering complexity. Every meaningful action needs a typed tool, validation, policy evaluation, persistence, and reason code, so adding new negotiation capabilities is slower.

## 8. Require written recap delivery before treating a verbal agreement as committed.  `T+23:15`

**Options considered**

- Treat verbal confirmation during the call as immediately binding
- Treat manager approval as the final commitment
- Create a verbal pre-agreement and promote it only after the written recap is accepted by the email provider

**Chosen:** A successful award call creates only a verbal pre-agreement. The commitment becomes committed only after the official recap email receives a sent result from the notification provider.

**Why:** This creates a durable written record of the agreed terms and prevents an ambiguous voice exchange from becoming the final system state. The compromise is dependence on the email provider: a valid telephone agreement can remain incomplete because of missing contact information, configuration errors, or delivery failures. Furthermore, sent currently proves provider acceptance, not final inbox delivery.

## 9. Store proposals, decisions, and evidence append-only instead of overwriting changed information.  `T+23:15`

**Options considered**

- Keep only the latest carrier quote
- Update the existing quote whenever the carrier changes a term
- Create a new record for every materially different proposal and link superseded versions

**Chosen:** Every materially changed proposal creates another quote and decision record. Earlier proposals remain stored and are linked through supersession rather than being replaced.

**Why:** This preserves exactly what was said and makes later decisions auditable under the mandate that existed at the time. The compromise is increased database and application complexity. Ranking must distinguish live from superseded quotes, the dashboard must present history without confusing the operator, and storage grows faster than a latest-state-only design.

## 10. Use a single bounded renegotiation round before producing the final comparison.  `T+23:16`

**Options considered**

- Accept the first quotations without renegotiating
- Continue calling carriers until no further improvement is possible
- Perform one first quotation round, one renegotiation round, and then stop for human approval

**Chosen:** Call all eligible carriers for initial proposals, call every first-round participant once more for its best final proposal, and then compare the live final quotes.

**Why:** One bounded round provides real negotiation while keeping cost, duration, concurrency, and state transitions predictable. The compromise is that the system may stop before reaching the theoretical best price. More rounds could improve terms, but they could also create indefinite negotiation loops, repeated calls, stale proposals, and an unpredictable operator experience.

## 11. Restrict automatic comparison to a safely comparable currency rather than inventing foreign-exchange conversions.  `T+23:17`

**Options considered**

- Compare the numeric amounts without considering currency
- Ask the language model to estimate exchange rates
- Fetch live foreign-exchange rates during every decision
- Require approved FX evidence or escalate mixed-currency proposals

**Chosen:** Single-currency comparison is the safe default. Foreign or mixed-currency proposals without an approved FX snapshot are escalated and cannot automatically win.

**Why:** This prevents the model or application from inventing an exchange rate and making an unreproducible financial decision. The compromise is reduced market coverage: legitimate foreign-currency quotations require manual handling or additional FX infrastructure before they can participate in automatic ranking.

## 12. Represent pickup commitments primarily as calendar dates rather than precise timestamps when carriers only negotiate a day.  `T+23:18`

**Options considered**

- Require an exact pickup timestamp from every carrier
- Silently assign a default time to date-only proposals
- Treat date-only and timestamp-specific pickup commitments as distinct evidence forms

**Chosen:** Preserve date-only pickup commitments without inventing a clock time, while still supporting explicitly stated times when evidence contains them.

**Why:** Carriers frequently negotiate pickup by day rather than by exact hour. Inventing midnight, noon, or another default caused valid quotations to fail policy checks against times nobody had spoken. The compromise is lower scheduling precision: date-only commitments cannot automatically resolve dock-hour or same-day timing conflicts without further clarification.

## 13. Leave the dashboard without real authentication during the demo build.  `T+23:18`

**Options considered**

- Implement full user authentication and role-based access control
- Require a shared portal bearer token
- Run an unauthenticated portal and record a configured manager identity as an audit label

**Chosen:** The portal is currently unauthenticated. Actions record PORTAL_MANAGER_IDENTITY, or portal-operator when it is not configured.

**Why:** This kept the dashboard usable during rapid local development and avoided authentication setup becoming a blocker for the core telephone workflow. The compromise is significant: the identity is an audit label, not proof of who performed the action. Anyone who can reach the deployed portal could potentially operate its management endpoints, so the current design is appropriate only for controlled demo access, not an exposed production deployment.

## 14. Keep call recording disabled by default and require an explicit consent notice before enabling it.  `T+23:19`

**Options considered**

- Record every call automatically for maximum evidence
- Never record calls and rely exclusively on transcripts
- Make recording opt-in and fail configuration when no consent notice is supplied

**Chosen:** Recording defaults to off. Enabling it requires an explicitly configured recording consent notice.

**Why:** This reduces privacy, consent, retention, and jurisdictional risk until those policies are deliberately approved. The compromise is weaker evidence when recording is disabled: transcripts and extracted offsets may exist, but operators may not have an audio file for replaying a disputed term. It also reduces the completeness of commitment emails that would otherwise include a recording link.

## 15. Return HTTP 200 with a structured error when a Vapi tool operation fails closed.  `T+23:19`

**Options considered**

- Return conventional HTTP 4xx or 5xx responses for failed tool operations
- Retry failed tool operations automatically
- Return HTTP 200 with an explicit error result that instructs the agent to hold or escalate

**Chosen:** Vapi-facing tool endpoints return HTTP 200 even when the requested action is refused or fails, with a structured error message that prevents the conversation from treating the operation as successful.

**Why:** Vapi may ignore non-200 tool responses, which could leave the language model continuing without receiving the refusal and therefore fail open conversationally. Returning a handled response ensures the model receives the restriction. The compromise is unconventional HTTP semantics: infrastructure monitoring cannot treat every 200 response as a successful business operation, and observability must inspect the response body and stored decision records to distinguish success from refusal.

## 16. OpenAI Model Selection  `T+23:43`

**Options considered**

- - gpt-4.1-mini
- - gpt-4.1

**Chosen:** gpt-4.1

**Why:** EVen though the mini model is supposed to be faster, the latency between both models was very similar, while gpt-4.1 having much more intelligence and generated much better responses.
