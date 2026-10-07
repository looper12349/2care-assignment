# Carepath frontend

The Next.js and TypeScript frontend runs locally on http://127.0.0.1:3000.

```sh
npm install
npm run dev
```

Start the Python conversation service first. The server-side API proxy uses
`CONVERSATION_URL`, which defaults to `http://127.0.0.1:8001`.

Patient chat, staff handoffs, feedback, evaluation results, and behavior activation
all use the Python service. The browser does not decide whether a booking is safe
or whether an improvement may be activated. Synthetic session tokens stay in
memory; local storage contains only the selected fictional patient and conversation
reference. This is a demonstration identity system, not production authentication.

```sh
npm run typecheck
npm run build
npm start
```

Evaluation uses LangSmith locally by default. Cloud upload is disabled in the UI.
The dashboard distinguishes candidate acceptance from explicitly activating it for
new conversations. Existing conversations retain their pinned behavior version.
