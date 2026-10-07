# Recorded demo and walkthrough

The repository includes an actual **41.5-second edited GIF** at
[`assets/carepath-demo.gif`](assets/carepath-demo.gif), recorded from Safari
against the running PostgreSQL-backed application on local port 3100.
It shows a fresh request, slot selection, exact confirmation, booking receipt,
patient feedback, a human handoff, staff acceptance, and a fresh three-repetition
LangSmith improvement run. The held-out failure and bounded artifact are visible,
followed by the measured 60/66 → 66/66 result. Behavior v2 was already active when
this capture began; the GIF does not claim to record a new activation click.

The edit crops browser chrome, adds captions, and shortens pauses. Application
results are unchanged. There is no audio or separately uploaded Loom recording.
All identities and scheduling records are synthetic. The GIF and synthetic
evidence are included in GitHub at the user's request; no datasets or patient
traces were uploaded to LangSmith.

The source scene list is [`assets/demo-timeline.json`](assets/demo-timeline.json).
The original browser captures remain in ignored `.data/demo-frames/`. To rebuild
the edit from those captures, run:

```sh
uv run --with pillow python scripts/render_demo.py
```

The script validates scene count, dimensions, and duration. Public evidence from
the fresh recorded run is in [`evidence/evaluation-summary.json`](evidence/evaluation-summary.json),
[`evidence/improvement.json`](evidence/improvement.json), and the self-contained
[`evidence/evaluation-report.html`](evidence/evaluation-report.html).

For a longer narrated submission, follow the script below. Keep it around five
minutes and use synthetic patients only.

## 1. Show the application and a full conversation

Start `./scripts/dev` and open http://localhost:3000. Explain that Next.js, Conversation, Scheduling, and Evaluation run as separate services. The default interpreter is deterministic, and LangSmith evaluation stays local.

Select Maya. Ask: “I need a routine primary care appointment tomorrow after 3 pm.” Select an available option, inspect its date/time/provider, and confirm the exact preview. Show the appointment receipt. Optionally submit a rating/comment to demonstrate that feedback is stored separately from booking memory.

Use another fresh conversation to ask for a person. Show the handoff ticket, open the staff view, and move it from queued to review. Explain that queued means a request exists, not that a person is connected.

## 2. Run the actual before/after experiment

Open the evaluation view, choose **3 runs per scenario**, and select **Run improvement loop**. This starts a durable background job. The worker executes baseline and candidate in isolated synthetic clinics; it does not book appointments in the patient demonstration's scheduling database.

The same experiment is available from a terminal:

```sh
./scripts/evaluate --repetitions 3
```

There are 22 complete conversation scenarios, including two held-out variations, giving 66 runs per version. The already measured deterministic result is baseline 60/66 and candidate 66/66 with zero critical violations and no regressions. Show the fresh run's actual values, even if they differ.

## 3. Explain the failure and structured improvement

Show the slot-conflict row in the comparison. The baseline safely hands off after another patient takes the selected slot; that fails the desired resolution check without violating booking safety.

Show the structured improvement. It is generated from observed failure records and can only enable one availability refresh, preserve the request, and ask for fresh confirmation. It cannot change identity checks, capacity rules, or emergency policy. It enables an existing reviewed branch; it does not train a model or invent new code.

Use the downloaded JSON report or generated `report.html` to show evidence from the failed baseline and passing candidate. Explain why a successful-sounding transcript would not be enough: the evaluator also checks appointments, operation receipts, handoff records, and event order. Show the timeout cases still passing: an uncertain write is looked up rather than treated as a lost slot.

## 4. Show acceptance and deliberate activation

Show every acceptance gate and the absence of regressions. Select **Use accepted version for new chats**. The Conversation service recomputes the gates and activates v2 for new conversations. Existing conversations retain their pinned version.

The CLI alone does not activate patient behavior. Use the UI job for this activation step. Once activated, another evaluation run still compares the fixed v1 baseline with v2; it does not replace the baseline with whichever version is currently active.

## 5. State the limits plainly

The demonstration proves a bounded recovery loop against synthetic scenarios. It does not establish real-language-model reliability, clinical triage, or readiness for real patient data. Live LangChain mode requires model credentials and separate live evaluation. Docker already uses PostgreSQL coordination; the SQLite launcher is a single-host fallback. Production still needs verified identity, a real clinic adapter, clinic-approved policy, operating limits, retention, and load testing.

Show the one-page design note and README. Mention AI assistance and the user's architecture choices. The user authorized publishing this repository and GIF to `looper12349/2care-assignment`; no live public clinic service is deployed by that push.
