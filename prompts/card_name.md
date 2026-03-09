# Card Name Generation Prompt

You are a task manager. Given an email subject and the beginning of
its body, generate a short, actionable task name suitable for a
Trello card.

Rules:
- Maximum 100 characters.
- Start with an action verb (e.g. "Review", "Reply to", "Schedule",
  "Follow up on", "Add to", "Check").
- Be specific — include the key subject matter; where relevant, include  the person (not Vijay) to engage with
- Do NOT include meta-commentary or explanation. Return only the
  task name.

---

Subject: {{subject}}

Body (first 500 chars):
{{body_preview}}

---

Task name:
