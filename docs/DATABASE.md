# Esquema mínimo — propuesta de rediseño

**Esto es una propuesta, no lo que está desplegado.** `docs/DATA_MODEL.md` documenta el
esquema actual (30 tablas, en `supabase/migrations/`) y sigue siendo la referencia de lo que
corre hoy. Este documento describe a dónde iría si lo rehiciéramos, y por qué.

Trece tablas en vez de treinta, con las seis restricciones intactas. Cada columna está
justificada por el escenario que la necesita.

| | |
| --- | --- |
| Tablas hoy | 30 |
| Propuestas | 13 |
| Vacías hoy | 13 |
| Con escritor en código hoy | 5 |
| Restricciones que sobreviven | 6 |

---

## 1. La regla

El esquema actual se diseñó desde `BUILD.md` §3 y las decisiones aprobadas del log — es
decir, desde lo que estaba **escrito**, no desde lo que iba a correr. Trece tablas siguen en
cero y solo cinco tienen código que las llene.

Este rediseño invierte el criterio:

> Una tabla existe si alguien la escribe. Cada tabla se justifica por un escritor concreto
> (semilla o agente) y por un escenario del demo. Lo que no tiene escritor colapsa a
> columna, a JSONB, o desaparece.

---

## 2. Mundo — lo siembras tú

Existe antes de la primera llamada. El agente solo lee.

### `counterparties`
Carriers y cliente. Un solo campo distingue el tipo; no valen dos tablas.
*Absorbe: `counterparty_contacts`, `carrier_documents`, parte de `tenants`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `name` | text | Lo que el agente pronuncia por teléfono |
| `kind` | carrier \| client | La diferencia entre un carrier y el cliente es un valor, no un esquema |
| `phone` | text E.164 | Un teléfono por contraparte. Su índice único es cómo se reconoce una llamada entrante |
| `is_on_file` | boolean | Si es falso el agente se niega a cotizar. Volta no da de alta a nadie por teléfono, y negarse es la conducta correcta |
| `docs` | jsonb | RFC, permiso SICT, seguro. Ningún flujo consulta un documento suelto |
| `persona` | text | "barato y lento", "no contesta". Sin personalidades en conflicto la comparación no demuestra nada |

`unique (phone)` — correlación de llamadas entrantes.

### `operations`
El embarque. Lleva el mandato, los relojes y la fase de mercado como columnas.
*Absorbe: `mandates`, `operation_clocks`, `rfqs`, `appointments`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `reference` | text | El folio. También es prueba de identidad nivel 1: solo una parte lo conoce |
| `client_id` | → counterparties | Quién tiene la carga y el dinero |
| `status` | text | draft · sourcing · awarding · booked · closed |
| `cap_amount` | bigint | En centavos, entero. Nunca flotante: el redondeo debe ser una decisión explícita |
| `cap_currency` | char(3) | Siempre explícita. Un monto sin moneda es un error esperando |
| `window_start` / `_end` | timestamptz | La ventana autorizada. Fuera de ella no hay permiso |
| `market_phase` | text | open · awarding · closed. Reemplaza `rfqs`: hay un solo RFQ por operación |
| `free_days` | int | Días libres antes de que corra demurrage |
| `last_free_day` | date | La cuenta regresiva del dashboard. Es lo que hace urgente todo lo demás |
| `discharged_at` | timestamptz | Arranca demurrage. Nadie lo decide: empieza solo |
| `gate_out_at` | timestamptz | Para demurrage y arranca detention |
| `payload` | jsonb | Contenedor, BL, pedimento, terminal, destino. Lo único con vocabulario logístico |

`check` — dígito verificador ISO 6346 sobre `payload.container_number`.

### `rate_cards`
Tarifas mock consistentes entre llamadas. *Absorbe: `lanes`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `counterparty_id` | → counterparties | De quién es la tarifa |
| `origin` / `destination` | text | Dos columnas en vez de una tabla `lanes` de una fila |
| `base_amount` | bigint | Centavos |
| `currency` | char(3) | — |
| `lead_time_hours` | int | Cuánto tarda de verdad. Sin esto no hay comparación por costo esperado |
| `reliability_bps` | int 0–10000 | Convierte una ventana prometida en costo esperado. Es lo que hace que el flete más barato pueda ser la operación más cara |
| `answers` | boolean | El carrier que no contesta. Es un escenario, no un defecto |

---

## 3. Durante la llamada — lo escribe el agente

Se escriben **en cola, nunca esperando**: la base de datos no está en el camino conversacional.

### `calls`
Una llamada. Lleva la grabación como columnas porque hay una sola por llamada.
*Absorbe: `recordings`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `provider_call_id` | text | El CallSid de Twilio con nombre neutro. El núcleo no debería saber qué proveedor existe |
| `direction` | inbound \| outbound | — |
| `operation_id` | → operations | A qué embarque pertenece. Nulo mientras no se correlacione una entrante |
| `counterparty_id` | → counterparties | Quién llamó. Nulo si el número no está en ficha — eso ya es información |
| `clock_reference_at` | timestamptz | **El** reloj. Todo offset se mide desde aquí; sin una referencia única las pistas no se alinean |
| `recording_url` | text | Dónde está el audio |
| `recording_offset_ms` | int | Inicio de grabación menos `clock_reference_at`. Sin esta resta, "reproducir el momento" cae en el lugar equivocado — peor que no reproducir nada |
| `status` | text | active · ended · failed |
| `from_number` / `to_number` | text | Buscar evidencia por quién llamó, no solo por un id opaco |

`unique (provider_call_id)` — un webhook reentregado no crea otra llamada.

### `utterances`
Cada frase transcrita. Append-only. *Renombra `call_transcript_events`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `call_id` | → calls | — |
| `seq` | bigint | Orden dentro de la llamada |
| `offset_ms` | int | Milisegundos desde `clock_reference_at`. Es el ancla de toda la cadena de evidencia |
| `speaker` | caller \| agent | — |
| `text` | text | — |
| `is_final` | boolean | Interino contra definitivo. Nunca se actúa sobre un interino, solo se muestra o se interrumpe con él |
| `confidence` | real | Deepgram la devuelve. Un número con baja confianza es motivo para preguntar, no para extraer |
| `event_key` | text | Idempotencia. Un frame reenviado no debe crear otra fila |

`unique (event_key)` · `unique (call_id, seq)` · `revoke update, delete`

### `offers`
Lo que un carrier dijo que haría y por cuánto. Una oferta que cambia es fila nueva.
*Absorbe: `offer_cost_components`, `participant_segments`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `operation_id` | → operations | Reemplaza `rfq_id`. El índice de adjudicación única funciona igual |
| `counterparty_id` | → counterparties | Quién ofreció |
| `call_id` / `offset_ms` | → calls, int | En qué llamada y en qué segundo exacto se dijo. Es la evidencia de la oferta |
| `amount` | bigint | Centavos |
| `currency` | char(3) | Se preserva la moneda original siempre, aunque se compare en otra |
| `is_total_final` | boolean | Por defecto **falso**, para que el silencio bloquee. "Más casetas" significa que el total no es final, y un total no final no se puede autorizar |
| `breakdown` | jsonb | El desglose si lo dan. En JSONB porque el demo no lo consulta línea por línea — solo necesita saber si está acotado |
| `pickup_start` / `_end` | timestamptz | La ventana ofrecida, que se compara contra la del mandato |
| `status` | text | proposed · superseded · withdrawn · accepted · rejected |
| `superseded_by` | → offers | Dijo 8,500 y luego 9,200. Los dos se dijeron. Sobrescribir borra justo el hecho que el jurado va a probar |
| `claimed_identity` | text | Quién *dijo* ser el que aceptó. Nunca se confía; se guarda |
| `identity_level` | smallint 0–3 | Qué tan establecido está. Llega a la política como dato y solo puede **exigir más**, nunca conceder más |

`unique (operation_id) where status = 'accepted'` — **invariante #5**.

### `decisions`
Cada evaluación de política, incluidos los rechazos. Append-only.
*Renombra `policy_decisions`; absorbe la necesidad de versionar mandatos.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `operation_id` / `call_id` / `offer_id` | uuid | Qué se evaluó y en qué contexto |
| `proposal` | jsonb | Copia de la entrada. Sin ella la decisión no se puede reproducir |
| `verdict` | text | allow · deny · clarify · escalate |
| `reason_code` | text | Por qué. Es literalmente lo que se le enseña al jurado cuando el agente se niega |
| `cap_at_decision` | bigint | **El tope copiado por valor.** Es el truco que permite que el mandato sea columnas: aunque alguien lo cambie después, la decisión de hace diez minutos sigue siendo explicable |
| `identity_level` | smallint | Contra qué nivel se juzgó |
| `decided_at` | timestamptz | — |

`revoke update, delete` — ni el backend puede reescribir sus propios rechazos.

### `events`
Bitácora append-only. Todo camino que muta pasa por aquí, y por eso la idempotencia vive aquí.
*Renombra `ledger_events`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `operation_id` / `call_id` | uuid | — |
| `type` | text | — |
| `payload` | jsonb | — |
| `idempotency_key` | text | Con `on conflict do nothing` es atómico: un webhook reentregado no tiene ventana para colarse |

`unique (idempotency_key)` — **invariante #7**, en una línea.

---

## 4. Después de la llamada

Sin presupuesto de latencia. Puede ser lento y usar un modelo con libertad.

### `commitments`
Una obligación autorizada y con evidencia. La evidencia son columnas, no tabla.
*Absorbe: `evidence`, `participant_segments`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `operation_id` / `offer_id` | uuid | Qué se cerró y sobre qué oferta |
| `state` | text | VERBAL · RECAP_SENT · COMMITTED · RESOURCED · DOCUMENTED · EXECUTED. Los tres últimos llegan horas después, fuera de la llamada |
| `evidence_call_id` | → calls | — |
| `evidence_offset_ms` | int **NOT NULL** | **Este NOT NULL reemplaza un trigger de quince líneas.** Si no puede existir un compromiso sin offset, no hace falta vigilar que no aparezca |
| `claimed_identity` / `identity_level` | text, smallint | Quién aceptó. El teléfono cambia de manos a media llamada, y el compromiso es de quien lo dijo |
| `superseded_by` | → commitments | La renegociación crea uno nuevo; no edita el anterior |

`unique (operation_id) where state not in ('SUPERSEDED','NOT_COMMITTED')` — un solo compromiso vivo.

### `commitment_transitions`
Cómo llegó a donde está, y qué autorizó cada paso. Append-only.

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `commitment_id` | → commitments | — |
| `from_state` / `to_state` | text | Una columna de estado dice dónde estás; esto dice cómo llegaste, incluido "el recap falló y por eso no se comprometió" |
| `decision_id` | → decisions | Qué evaluación autorizó este paso concreto. Nulo cuando el paso lo dispara un hecho del mundo y no una autorización |
| `occurred_at` | timestamptz | — |

`revoke update, delete`

**Por qué es tabla propia y no parte de `decisions`** — decidido, ver `DECISION_LOG.md`
D-DB-05. En resumen: una decisión dice *"esto sería permitido"* y una transición dice
*"esto pasó"*. La cardinalidad no cuadra (≈20 decisiones producen 1 transición, y 3 de cada
4 transiciones no tienen decisión detrás: las disparan el webhook de Resend, la asignación
de unidad y el gate-out), los escritores son distintos, y fusionarlas invita a tratar
`allow` como equivalente a "quedó cerrado" — que es el bug exacto que la arquitectura existe
para prevenir.

### `call_reports`
Lo que el modelo entendió de una llamada terminada. *Fusiona `call_recaps` + `call_briefs`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `call_id` | → calls, pk | Uno por llamada |
| `summary` | text | — |
| `quoted_prices` / `objections` / `conditions` | jsonb | Lo mencionado |
| `actions` / `mentions` | jsonb | Lo que era el brief. Se escribe en el mismo momento y por el mismo código que el recap: misma razón para cambiar, mismo nivel de confianza, una sola tabla |
| `agreement_candidates` | jsonb | Acuerdos *candidatos*. El modelo propone; la política decide si alguno se vuelve compromiso |
| `model` | text | Qué modelo lo escribió, para poder reproducirlo |

### `notifications`
El recap escrito. Su entrega es lo que deja pasar un compromiso a COMMITTED.
*Renombra `call_recap_deliveries`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `call_id` / `commitment_id` | uuid | Qué se está confirmando |
| `channel` | email \| sms | — |
| `to_address` | text | — |
| `status` | pending \| sent \| failed | `sent` es la compuerta. `failed` significa que **no hubo compromiso**, no que hubo uno defectuoso |
| `provider_message_id` | text | Idempotencia del webhook de entrega |

### `handoffs`
Lo que el agente se negó a decidir solo.
*Unifica `escalations` + `call_handoffs` + `call_handoff_events`.*

| Columna | Tipo | Por qué |
| --- | --- | --- |
| `id` | uuid pk | — |
| `call_id` / `operation_id` | uuid | — |
| `reason` | text | Fuera de mandato, identidad insuficiente, contradicción |
| `context` | jsonb | Suficiente para que un humano tome una llamada viva sin leer una transcripción |
| `raised_at` / `accepted_at` | timestamptz | Cuánto tardó un humano en tomarla. Es una métrica del demo |

---

## 5. Tablas a considerar, y el escenario que las obliga

Ninguna es necesaria para los cuatro escenarios base. Cada una entra solo si su disparador
entra al guion.

| Tabla | Entra si… | Nota |
| --- | --- | --- |
| `mandates` versionada | un humano cambia el tope a media demo, o quieren mostrar el historial en el dashboard | Si vuelve, el estado activo va en `status`, **no** en `superseded_by`: con lo segundo la sustitución se traba — insertar la v2 viola el índice y actualizar la v1 primero viola la llave foránea |
| `participant_segments` | hacen el escenario "me pasan al dueño" y quieren la línea de tiempo completa | Sin ella sabes *quién aceptó*, pero no la secuencia de quién habló en qué tramo |
| `fx_rate_snapshots` | el mandato vuelve a ser en USD | **Obligatoria** en ese caso, no opcional: una cifra convertida sin la tasa que la produjo no es verificable |
| `offer_cost_components` | van a enseñar la suma línea por línea, o a filtrar por categoría de cargo | El JSONB `breakdown` alcanza para saber si el total está acotado |
| `tool_invocations` | el pitch dice "p95 bajo 200 ms" y hay que enseñarlo | Es telemetría, no estado. Si no, va a logs |
| `counterparty_contacts` | un carrier tiene varios teléfonos, o "te llamo de vuelta" usa un número distinto al de ficha | Con un teléfono por contraparte, una columna con índice único basta |
| `scenarios` / `playbook` | quieren defender la generalidad ante el jurado | La única que no sirve al demo sino al pitch. Solo vale si de verdad hay dos escenarios compartiendo maquinaria |

---

## 6. Lo que no se pierde

Las seis restricciones son el activo real del diseño. Caben igual en trece tablas que en
treinta, y una se vuelve más simple.

| Invariante | Cómo queda | Cambio |
| --- | --- | --- |
| Una sola adjudicación | `unique index on offers (operation_id) where status = 'accepted'` | apunta a la operación en vez del RFQ |
| Idempotencia | `unique (idempotency_key)` + `on conflict do nothing` | igual |
| Append-only | `revoke update, delete` en decisions, events, transitions, utterances | igual |
| Sin evidencia no hay compromiso | `commitments.evidence_offset_ms NOT NULL` | **el trigger desaparece** |
| Un compromiso vivo | índice único parcial sobre `operation_id` | igual |
| Decisión explicable | `decisions.cap_at_decision` copiado por valor | reemplaza el versionado de mandatos |
