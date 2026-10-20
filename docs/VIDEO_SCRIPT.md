# TuitionPilot: demo video script (4 min 50 s)

Screen recording of the live site at 1920×1080, English voice-over. About 640 words at a calm pace. Each block: what to do on screen, then what to say.

**Before recording:** open the site in a fresh browser window, full screen, zoom 100%, bookmarks bar hidden, notifications off. Start on the Home page at the top. Recorder: macOS Shift+Cmd+5, Windows Win+Alt+R, or OBS. Export MP4, under 5 minutes.

---

### 1. Hook · 0:00–0:20 · Home, top of the page
**Screen:** stay on the hero; let the currency chip rotate once.
**Say:** "Every year thousands of students from Central Asia pay tuition to universities in China. One wrong payment method, one late transfer or one fake email can cost a semester. This is TuitionPilot, an AI payment agent built for exactly this job."

### 2. Who it is for · 0:20–0:40 · Home
**Screen:** scroll to "Who are you?", click Student, then Parent or sponsor. Scroll to "Before you pay tuition: a 6-point check" and answer two questions.
**Say:** "It works for the student, for a parent paying from home, and for the university office. Before any money moves, it asks the questions students usually forget: is this method accepted, is there a payment reference, when must the money be credited."

### 3. Plan my payment · 0:40–1:45 · Plan my payment
**Screen:** click "Plan my payment" in the menu, then the preset "Normal month". Click "Why this plan", then "All 20 plans", then "Money by date".
**Say:** "Here is Aigerim. She must pay forty-five thousand yuan by the third of September and keep half a million tenge for rent. The agent builds twenty plans: five routes times four schedules. Hard limits come first: a route that is late, uses an old price or breaks her cash cushion is out, however cheap it is. Nine plans pass. The cheapest safe one costs about fifty-seven thousand tenge less than a bank wire, in our simulation. And this chart shows her balance never drops below the cushion."

**Screen:** click the preset "Sponsor pays late".
**Say:** "If her sponsor pays ten days late, no plan is safe, and the agent says INFEASIBLE instead of guessing."

**Screen:** click "Normal month" again, open "Approve and pay", click "Approve" and wait about 5 seconds while the statuses fill in up to "Credit matches: paid". Then click "Change amount by +1,000 KZT".
**Say:** "Aigerim approves this exact payment on one screen. Then we track it honestly: submitted and sent are not paid. Only a credit that matches amount, payee and reference closes the bill. And if anyone changes the amount by even a thousand tenge after approval, the fingerprint no longer matches and the approval is void."

### 4. Mira: the fake email · 1:45–2:25 · Student cases
**Screen:** open "Student cases", choose Mira, click "Use the fake email".
**Say:** "Mira gets an email that looks exactly like her university. It asks her to pay a new account. TuitionPilot compares every field with the version Mira confirmed by phone. The account, the bank and the country are different, so the payment is blocked. Hidden instructions to the AI inside the email are treated as text, not orders. Even a real change waits forty-eight hours until she confirms it through the official website."

**Screen:** click "Use the real invoice".
**Say:** "With the real invoice, everything matches and she can go on."

### 5. Li's mum pays from China · 2:25–3:05 · China corridors
**Screen:** open "China corridors" and scroll to "Same invoice, two directions". Point at Corridor B (red MISSING DOCUMENTS), then tick "Corridor B: the parent uploads proof of relationship" and let it turn READY.
**Say:** "China works in both directions. On the left, Aigerim pays a Chinese university from Kazakhstan. On the right, Li studies in Almaty and his mum pays from China. That is a different corridor with different rules. First the agent says a document is missing: proof that she is his parent. Once it is uploaded, the bank wire becomes eligible and the agent shows the last safe day to start. If a rule is out of date or comes from another university, the method is never marked as allowed."

### 6. Proof, not promises · 3:05–3:40 · Payment plan 2.0
**Screen:** open "Payment plan 2.0", scroll to the Evidence Pack, click "As exported", then "One item edited".
**Say:** "Every decision is recorded in a hash-chained ledger and packed into an evidence pack: the plan, the rules and their versions, the approval and the payment status. Change one item and the pack turns INVALID."

### 7. The agent cannot change its own rules · 3:40–4:05 · Safety
**Screen:** open "Safety". In the rule simulator choose L3 Auto, then "Raise its own limit". Then choose "Pay a bill" and tick "Stop button pressed".
**Say:** "The student chooses how much freedom the agent gets, from read-only to autonomous within a cap. The agent can never raise its own limit, and the stop button blocks everything at once."

### 8. Results · 4:05–4:35 · Results
**Screen:** open "Results", scroll slowly past the backtest and the Evaluation Lab.
**Say:** "On three hundred simulated exchange-rate paths the plan met every deadline and cut timing risk by fifty-nine percent. Our engine passes one hundred and four automated tests and all sixteen evaluation cases. And the site runs the same algorithm as our Python code: we compared them on thirty thousand plans with zero differences."

### 9. Close · 4:35–4:50 · Team
**Screen:** open "Team", show the GitHub link.
**Say:** "Everything here is a simulation; real money would move only through licensed partners. The code is open on GitHub. We are Borderless Scholars. Thank you."
