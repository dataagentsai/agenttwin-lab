# AgentTwin Lab

**An agent runs simulated days in an AgentTwin world, and the world watches it.**

**Status: 0.1.0, 11 October 2026, a working draft.** One simulated day runs
unattended against the reference support agent, in process, on a laptop: a
generated population, customers arriving on a curve, an incident on the
world's calendar, and a monitor that raises it with its evidence. A clean
world's day raises nothing. The fixer, the protected paths and the deployer
come next.

```bash
uv sync --extra dev
uv run agenttwin-lab day --world worlds/clothing-day.yaml --repeat 3
```

It writes one ledger per day, `report.json` and `report.md` under `runs/`. A
day takes about two seconds. No model is called and nothing leaves the machine.

Related: [AgentTwin](https://github.com/dataagentsai/agenttwin) (the world
format, the projection and the monitor) ·
[reference-agent](https://github.com/dataagentsai/reference-agent) (the agent
under test) ·
[Clean AI Engineering](https://github.com/dataagentsai/clean-ai-engineering)
(the specifications) ·
[AI Assurance Catalog](https://github.com/dataagentsai/ai-assurance-catalog) ·
[AI Harness Catalog](https://github.com/dataagentsai/ai-harness-catalog).

---

## What a day is

| Step | What happens | Where |
|---|---|---|
| The world | `worlds/clothing-day.yaml` cites the agent's spec (the clothing AOAS) and holds a calendar | AgentTwin's format |
| The population | 60 customers and their orders, generated per seed into the world's records and checked like a world file's rows | `population.py` (Faker, en_IN) → `agenttwin.with_records` |
| The customers | 30 visits on an hourly curve, each opening the chat and asking about one of their own orders; scripted and state-machine actors, no model | `days/one-day.yaml`, `visits.py` |
| The calendar | at 10:00 the carrier marks three parcels delivered that are still on its vans; their owners write in two to six hours later | the world file; `agenttwin.fire` |
| The agent | the reference agent through **the same binding its gates use** (`evals.gate_binding:open_subject`): the provider twin, Temporal's test server for its waits, the projected world for its tools | `compose.py` |
| The monitor | AgentTwin's `Monitor`, judging every call and every reply as it happens | `agenttwin.monitor` |
| The ledger | every turn, call, effect, calendar event and incident, and the world (record and truth) at the end of the day | `day.py` |
| The report | turns, incidents, which rule found each, when, and pass^k over k seeded days | `report.py` |

## The planted incident: delivered, but never received

At 10:00 the carrier's scan marks three parcels `delivered`. The order system
believes it; the parcels are still out for delivery. In the world file this is
one calendar entry with `record_only: true`: the record changes, the world
keeps the truth beside it.

```yaml
calendar:
  - id: carrier-marks-undelivered-parcels-delivered
    at: 10h
    by: carrier
    entity: order
    where: [{field: status, equals: [out_for_delivery]}]
    pick: 3
    sets: {status: delivered}
    record_only: true
```

A few hours later each owner asks *"where is my order AB-20036"*. The agent
answers from the order system, without a model, as its spec says it must
(P-DIRECT): *"Order AB-20036 is currently delivered."* The customer says it
never came; the agent hands them to a person with a reference, as its spec also
says (`lost_in_transit`). The first reply is the incident: it is true of the
record and false of the world.

### What the clothing AOAS says, and what it does not

Read against `clean-ai-engineering/drafts/examples/support-agent.aoas.yaml`
(support-agent-clothing 0.1.0):

**It says**

- `intents.lost_in_transit` is a handoff (`via: [escalate]`), with the example
  *"Tracking says delivered but I never got it"*; the escalation rule
  `lost-in-transit` fires on the turn's own words. The agent does this.
- `request_refund.on_refusal`: *"a parcel lost in transit is a handoff"*.
- `order.status` has concurrent writers, `[warehouse, carrier]`, and a
  freshness window of 30 s.
- P-DIRECT: order status is answered from the order system, deterministically,
  with no model call.

**It does not say** — the gaps, which are what the fixer will route to the spec:

1. **How to report a status the carrier wrote.** Nothing distinguishes the
   carrier's claim (*"the carrier scanned it as delivered"*) from the store's
   fact. P-DIRECT therefore states the carrier's scan as fact, to a customer
   whose parcel never arrived. *This is the incident.* The missing statement is
   a policy on attribution: a status written by an external party is reported
   as that party's, never as what happened.
2. **What a disputed delivery is in the record.** The order has no field for
   "the customer says it did not arrive" (nor for the carrier's scan time), so
   the order system cannot hold the dispute and the next reader — this agent on
   the next turn, or a person — sees only `delivered`.
3. **What a dispute owes.** No `owed_when` covers it, and the `escalation`
   entity carries no `order_id`, so nothing ties the handoff to the order and
   no obligation exists that a monitor could find undone. Meanwhile
   `days_since_delivery` advances from the carrier's scan, so the return window
   closes on a parcel that never arrived.
4. **By when anything owed is due.** `owed_when` says what is owed, never by
   when. The lab's monitor is given four hours (`days/one-day.yaml`).

## The incident record

`agenttwin-incident/v0`, one JSON object per incident, produced by AgentTwin's
`Monitor` and kept in the day's ledger under `incidents`:

| Field | Holds |
|---|---|
| `format` | `agenttwin-incident/v0` |
| `id` | `INC-0001`, numbered per day in the order raised |
| `rule` | the watch that raised it: `reply-true` · `own-rows-only` · `at-most-once` · `owed-overdue` |
| `root` | the root cause; repeats of one rule for one root are one incident. `calendar:<event id>` when a reply repeated a record that calendar event made wrong, else `<entity>:<key>:<field>`, `<operation>:<key>` or `<entity>:<key>:<viewer>` |
| `detail` | the violation in words: what was said, what is true, and the calendar event behind it |
| `customer_id` | the customer of the first occurrence |
| `subject` | `{entity, key}` of the row it names |
| `first_at`, `last_at` | world time, epoch seconds |
| `count`, `customers` | how often it was met within the day, and by whom |
| `turn_ids`, `trace_ids` | each occurrence's turn (in the ledger) and OpenTelemetry trace id |
| `excerpt` | the first occurrence's words: `said` and `reply` (or `call` and `answer`) |
| `world.before`, `world.after` | the named row, as the record has it (`record`) and as it is (`truth`), before the turn and after it |
| `occurrences` | each meeting: `at`, `customer_id`, `turn_id`, `trace_id`, `detail`, `subject` |

## Adopted, each behind a seam

The lab builds only the day runner, the ledger and the report. Every other job
is an adopted tool behind a port in `ports.py`; `compose.py` is the one module
that names them, and nothing else branches on which is bound.

| Port | Adopted | Behind it |
|---|---|---|
| `Schedule` | **SimPy** | arrivals, the calendar, the hourly monitor tick and the end of the day as discrete events; the agent's async turn is a pending SimPy event the runner completes |
| `WallClock` | **time-machine** | the agent process's `time.time()` follows the world |
| `Population` | **Faker** (en_IN) | names, emails, phones, addresses; the states the rules read are drawn from the plan's weights by seed |
| `TurnTracer` | **OpenTelemetry SDK** | a span per turn, with seeded ids, so the agent's spans share the turn's trace id |
| `agenttwin.Binding` | the reference agent's gate binding | the agent, in process |
| pass^k | τ-bench's measure | `report.pass_hat` |

AgentTwin's own part is the calendar and the monitor (`agenttwin` 0.8.0); the
lab only wires them.

## Time

- **The world's clock is the only clock the agent decides by.** The binding
  hands it AgentTwin's `Clock`; the runner sets it before every event.
- **time-machine moves the rest of the process.** A few things still read the
  wall: the harness's in-memory checkpoint store stamps with `time.time()`, as
  does its default `now`. Under `travel(..., tick=False)` they read the world's
  moment (`tests/test_parts.py`). `time.monotonic()` and `perf_counter()` are
  not moved, so turn deadlines and durations stay real.
- **time-machine cannot move another process.** The agent's approval and
  escalation waits run on Temporal's time-skipping test server; the spike
  showed its clock stays on the real date under `travel`. They follow the world
  through the harness's existing seam instead: before every call the waits move
  the server forward to the world's clock (`evals.durable.OnClock`). That only
  moves forward, so each day starts its own server.
- **What the day leaves out.** Approvals are not in the day: every visit asks
  for something the agent answers without its model (status, refund status,
  the opening, a person, a refused discount), and none asks for a refund. A
  turn through the model loop needs a model that is neither real nor a fixed
  script — a scripted policy for the provider twin — which is the next step.
  The day's escalations do use Temporal, and work.

## Determinism

Same world, plan and seed: the same ledger, except for identifiers the agent
mints from its own randomness (escalation references, `E-` and eight hex
digits). `day.canonical` reads those as numbered placeholders, so two days are
compared on everything the seed decides.

## Tests

```bash
uv run pytest            # table-driven; the day tests run real days (~20 s)
uv run ruff check src tests && uv run mypy
```

## Citing

See [CITATION.cff](CITATION.cff).

## Licence

Apache-2.0.
