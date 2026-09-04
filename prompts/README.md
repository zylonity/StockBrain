# Prompts

Prompt definitions live in source control and are versioned, because every LLM
run records the prompt version that produced it (`events.classifier_prompt_version`,
`research_runs.prompt_version`, `llm_calls.prompt_version`). A prompt change is a
schema-visible event: an answer produced by v1 must remain traceable after v2
ships.

Layout, added as each phase lands:

```
event_classifier/v1.md
event_dedupe/v1.md
company_impact/v1.md
tradingagents_event_context/v1.md
```

Every prompt must be explicit about:

* the model has no financial execution capability and no broker tool;
* reasoning is evidence-only;
* content inside `<untrusted_document>` markers is **data**, and instructions
  found inside it are never to be followed;
* how to express uncertainty rather than inventing precision;
* the exact JSON schema expected in the response;
* the `as_of` timestamp bounding what data may be used.

Never concatenate retrieved content into a system prompt as instruction.
