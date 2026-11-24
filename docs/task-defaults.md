# Starting values of the New task form

Decision, 2026-11-24.

**Need.** A drafted task (the next unchecked item in STATUS.md) already brings
its title and description. Choosing the agent, model and reasoning effort by
hand every time was slow, and the form forgot the previous choice.

**Order.** The form starts with:

1. the recommendation written in the draft: a line
   `Recommended: <agent>, <model>, <effort>` in the item's description
   (`Recomendado:` also works; model and effort are optional);
2. otherwise, the agent, model and effort of the last local task;
3. otherwise, the first available agent with its CLI default.

**Who writes the recommendation.** The agent that finishes a task. Baton's
prompt lists the agents, models and efforts this computer offers, so the agent
picks among real options: cheaper, faster models for simple work, stronger ones
and more effort for complex or risky work.

**Checked on the server.** Before the form shows a recommendation, Baton checks
that the agent is installed and the model and effort are in the CLI's own list.
If the agent is missing, the form falls back to the last choice; if only the
model or effort is missing, it keeps the agent with the CLI default. Either way
it says which recommendation was not available, instead of failing silently.

**Why the last choice comes from the task history and not the browser.**
Baton is used from the PC and from a phone. Taking it from `tasks.json` gives
the same starting point on every device, and needs no new stored state.

**Trade-off.** The recommendation is the previous agent's judgment, not a rule.
It is a starting value: everything stays editable before sending.
