// JSON-Schema objects that drive every agent({schema}) return in the
// research-batch workflow. Plain objects, no imports. The workflow inlines a
// copy of these (exports stripped); test/contract.test.js guards the copy.
//
// Each schema is matched to the CANONICAL agent it validates:
//   SEARCH          ← researcher:search-agent
//   SUMMARIES       ← researcher:fetch-summarize-runner (this repo's new agent)
//   HOP_NEXT        ← researcher:hop-planner   (nullable next_hop / replan_hint)
//   CLASSIFY        ← librarian:classify-agent (neutral input contract)
//   WIKILINK_RESULT ← librarian:wikilink-scanner (an edit PLAN: {edits, stats})
//   MOC_RESULT      ← optional serialized MOC step (defensive)
//   THREADS         ← researcher:thread-discoverer
// `additionalProperties` is left default (true) — the agents return richer
// objects than the required keys; only the load-bearing keys are pinned.

// researcher:search-agent — per-topic web search + T1–T4 source scoring.
export const SEARCH = {
  type: 'object',
  required: ['selected_urls'],
  properties: {
    topic: { type: 'string' },
    depth: { type: 'string' },
    queries_used: { type: 'array', items: { type: 'string' } },
    selected_urls: {
      type: 'array',
      items: {
        type: 'object',
        required: ['url', 'tier', 'is_primary', 'credibility_score'],
        properties: {
          url: { type: 'string' },
          title: { type: 'string' },
          tier: { type: 'string' },
          is_primary: { type: 'boolean' },
          primary_type: { type: ['string', 'null'] },
          credibility_score: { type: 'number' },
          relevance_score: { type: 'number' },
        },
      },
    },
  },
};

// researcher:fetch-summarize-runner — fetch + summarize a topic-hop's URLs.
export const SUMMARIES = {
  type: 'object',
  required: ['items'],
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        required: ['url', 'summary', 'tier', 'is_primary'],
        properties: {
          url: { type: 'string' },
          title: { type: 'string' },
          summary: { type: 'string' },
          source_type: { type: 'string' },
          key_entities: { type: 'array', items: { type: 'string' } },
          key_claims: { type: 'array', items: { type: 'string' } },
          tier: { type: 'string' },
          is_primary: { type: 'boolean' },
          credibility_score: { type: 'number' },
          fetch_status: { type: 'string' },
        },
      },
    },
    // needs_browser pages the runner could not read; surfaced to the user.
    manual_urls: {
      type: 'array',
      items: {
        type: 'object',
        required: ['url'],
        properties: { url: { type: 'string' }, reason: { type: 'string' } },
      },
    },
  },
};

// researcher:hop-planner — full decision object. The JS loop computes the
// authoritative continue/stop/replan via confidence.decide(); this validates
// the agent's canonical output, whose next_hop is null on stop and is replaced
// by replan_hint on replan.
export const HOP_NEXT = {
  type: 'object',
  required: ['decision', 'self_reflection'],
  properties: {
    topic: { type: 'string' },
    current_hop: { type: 'number' },
    decision: { type: 'string', enum: ['continue', 'stop', 'replan'] },
    confidence_score: { type: 'number' },
    contradiction_rate: { type: 'number' },
    next_hop: {
      type: ['object', 'null'],
      properties: {
        pattern: { type: 'string' },
        from: { type: 'string' },
        rationale: { type: 'string' },
        candidate_score: { type: 'object' },
      },
    },
    replan_hint: { type: ['object', 'null'] },
    self_reflection: { type: 'string' },
  },
};

// librarian:classify-agent — neutral input contract. The workflow consumes
// notes_to_create; vault_context + contradictions ride through to the writer.
export const CLASSIFY = {
  type: 'object',
  required: ['notes_to_create'],
  properties: {
    topic: { type: 'string' },
    notes_to_create: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'content', 'priority', 'action'],
        properties: {
          title: { type: 'string' },
          content: { type: 'string' },
          frontmatter_meta: { type: 'object' },
          citations: { type: 'array' },
          link_hints: { type: 'array', items: { type: 'string' } },
          priority: { type: 'string' },
          action: { type: 'string' },
        },
      },
    },
    vault_context: { type: 'object' },
    contradictions_detected: { type: 'array' },
  },
};

// librarian:wikilink-scanner — an edit PLAN (the caller applies the edits).
export const WIKILINK_RESULT = {
  type: 'object',
  required: ['edits', 'stats'],
  properties: {
    edits: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'find', 'replace'],
        properties: {
          file: { type: 'string' },
          find: { type: 'string' },
          replace: { type: 'string' },
          context: { type: 'string' },
          direction: { type: 'string' },
        },
      },
    },
    stats: { type: 'object' },
  },
};

// Optional serialized MOC-update step (defensive — present if the workflow
// runs a standalone MOC pass rather than letting the librarian skill own it).
export const MOC_RESULT = {
  type: 'object',
  required: ['updated'],
  properties: {
    updated: { type: 'array', items: { type: 'string' } },
    skipped: { type: 'array', items: { type: 'string' } },
  },
};

// Librarian skill-writer return — the notes actually written + MOC/back-link
// updates. WITHOUT this schema agent() returns the writer's free text (prose +
// JSON), and `.written_notes` read off a string is undefined, so the workflow
// under-reports as 0 even when notes were written. The schema forces a parsed
// object. (Found by the first real e2e, run wf_2266c970-a27.)
export const WRITE_RESULT = {
  type: 'object',
  required: ['written_notes'],
  properties: {
    written_notes: {
      type: 'array',
      items: {
        type: 'object',
        required: ['path', 'title', 'action'],
        properties: {
          path: { type: 'string' },
          title: { type: 'string' },
          action: { type: 'string' },
        },
      },
    },
    updated_notes: { type: 'array', items: { type: 'string' } },
  },
};

// researcher:thread-discoverer — follow-up lead scoring for the thread gate.
export const THREADS = {
  type: 'object',
  required: ['threads', 'batch_stats'],
  properties: {
    project: { type: 'string' },
    threads_discovered: { type: 'number' },
    threads: {
      type: 'array',
      items: {
        type: 'object',
        required: [
          'topic',
          'score',
          'scoring_breakdown',
          'novelty_status',
          'rationale',
          'suggested_priority',
          'mentioned_in',
          'related_vault_notes',
        ],
        properties: {
          topic: { type: 'string' },
          score: { type: 'number' },
          scoring_breakdown: { type: 'object' },
          novelty_status: { type: 'string' },
          rationale: { type: 'string' },
          suggested_priority: { type: 'string' },
          mentioned_in: { type: 'array' },
          related_vault_notes: { type: 'array' },
        },
      },
    },
    batch_stats: { type: 'object' },
  },
};
