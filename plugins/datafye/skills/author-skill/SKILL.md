---
name: author-skill
description: Use when the user asks you to create, save, make, define, or "remember as a skill" a reusable procedure or workflow. Trigger phrases include "make a skill that...", "save this as a skill", "create a reusable workflow for X", "turn this into a skill I can run". Writes a new SKILL.md in the correct scope directory.
---

# Author a skill

Create a new reusable skill for the user. A skill is a `SKILL.md` file: YAML
frontmatter (a `name` and a `description`) followed by a markdown body of
instructions you will follow whenever the skill runs.

## Steps

1. **Clarify intent.** Confirm what the skill should do and, crucially, WHEN it
   should trigger. The `description` is the most important line — it is what
   decides when the skill gets used — so make it specific and include the
   phrases the user would actually say.

2. **Choose the scope** (ask the user if it is ambiguous):
   - **Reusable across ALL the user's strategies** -> user-global. Write to the
     user-global skills directory shown in your SKILLS context:
     `<user-global skills dir>/<skill-name>/SKILL.md`.
   - **Specific to the CURRENT strategy only** -> per-strategy. Write to
     `.claude/skills/<skill-name>/SKILL.md` (relative to this strategy folder,
     which is your current working directory).

3. **Pick a short kebab-case `<skill-name>`** (e.g. `momentum-setup`).

4. **Write the SKILL.md** with exactly this shape:
   ```
   ---
   name: <skill-name>
   description: Use when ... (specific situations and phrasing the user would say)
   ---

   # <Title>

   <Numbered, actionable steps to follow when this skill runs.>
   ```

5. **Confirm** to the user what you created, its scope, and how to run it (they
   just ask you to use the `<skill-name>` skill, or you will invoke it
   automatically when their request matches the description).

## Notes

- Never write to the system skills directory — those are read-only and managed
  by Datafye. You can only create user-global or per-strategy skills.
- A newly created skill becomes available on the user's next message (skills are
  reloaded each turn).
- Put the "when to use" cues in the `description`, not buried in the body — the
  description is matched against the user's request.
- Keep the body focused and actionable.
