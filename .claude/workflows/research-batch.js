// AUTO-ASSEMBLED workflow — the two library sections below are inlined verbatim
// from lib/confidence.js and lib/schemas.js (exports stripped). The Workflow
// runtime has no module/filesystem access, so this file carries zero
// import/require. test/contract.test.js guards that the inlined sections still
// match lib/ (regenerate by re-running the inline step if lib changes), and
// that the file obeys the Workflow launch contract.
//
// RUNTIME GLOBALS (provided by the Workflow tool — never imported):
//   agent(prompt, opts) · parallel(thunks) · log(msg)
// agent() has NO input channel — callAgent folds per-call data into the prompt.

export const meta = {
  name: 'research-batch',
  description: 'Researcher batch fan-out — runs the per-topic multi-hop research loop (search, fetch+summarize, JS confidence/decide/replan) in parallel over a large web_research batch, then delegates the write span to the Librarian plugin (classify, write, wikilink) and discovers follow-up threads. Dispatched by the /researcher skill only when a batch exceeds batch_threshold.',
  phases: ['Research', 'Write', 'Discover'],
};

// >>> BEGIN inlined lib/confidence.js (exports stripped; synced by test/contract.test.js) >>>
// Research quality scoring — JS twin of scripts/confidence.py.
//
// Pure functions for confidence + contradiction signals from a topic's sources,
// plus the hop-planner continue/stop/replan decision. No I/O, no imports.
// Kept value-for-value in sync with confidence.py via shared test vectors
// (test/confidence.parity.test.js). The research-batch workflow inlines a copy
// of these definitions (exports stripped); test/contract.test.js guards that
// the inlined copy matches this source.

const DEPTH_PROFILES = {
  quick:      { max_hops: 1, target_sources: 10, confidence_target: 0.6 },
  standard:   { max_hops: 3, target_sources: 20, confidence_target: 0.7 },
  deep:       { max_hops: 4, target_sources: 40, confidence_target: 0.8 },
  exhaustive: { max_hops: 5, target_sources: 50, confidence_target: 0.9 },
};
const TIER_WEIGHTS = { T1: 1.0, T2: 0.75, T3: 0.5, T4: 0.25 };

function getDepthProfile(name) {
  const p = DEPTH_PROFILES[name];
  if (!p) throw new Error(`unknown depth: ${name}`);
  return p;
}
function tierDiversityWeight(sources) {
  if (!sources.length) return 0;
  return sources.reduce((a, s) => a + (TIER_WEIGHTS[s.tier] ?? 0.25), 0) / sources.length;
}
function topicCoverage(sources) {
  const t2plus = sources.filter((s) => s.tier === 'T1' || s.tier === 'T2').length;
  return Math.min(1, t2plus / 3);
}
function primarySourcePresence(sources) {
  return Math.min(1, sources.filter((s) => s.is_primary).length / 2);
}
function sourceCountAdequacy(count, target) {
  if (target <= 0) return 0;
  return Math.min(1, count / target);
}
function computeConfidence(sources, depth) {
  if (!sources.length) return 0;
  const target = getDepthProfile(depth).target_sources;
  return (
    0.4 * tierDiversityWeight(sources) +
    0.3 * topicCoverage(sources) +
    0.2 * primarySourcePresence(sources) +
    0.1 * sourceCountAdequacy(sources.length, target)
  );
}
function contradictionRate(sources, contradictions) {
  if (sources.length < 2 || !contradictions.length) return 0;
  return Math.min(1, contradictions.length / (sources.length * 0.3));
}
function tierFromScore(score) {
  if (score >= 0.9) return 'T1';
  if (score >= 0.7) return 'T2';
  if (score >= 0.5) return 'T3';
  return 'T4';
}
// Hop-planner Step 3 rules, order-sensitive. confidence.py / SKILL Stage 5 parity.
function decide({ confidence, contradictionRate: cr, hop, maxHops, target }) {
  if (confidence >= target && cr <= 0.3) return 'stop';
  if (hop >= maxHops) return 'stop';
  if (confidence < target * 0.7 && hop === 1) return 'replan';
  return 'continue';
}
// <<< END inlined lib/confidence.js <<<

// >>> BEGIN inlined lib/schemas.js (exports stripped; synced by test/contract.test.js) >>>
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
const SEARCH = {
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
const SUMMARIES = {
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
const HOP_NEXT = {
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
const CLASSIFY = {
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
const WIKILINK_RESULT = {
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
const MOC_RESULT = {
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
const WRITE_RESULT = {
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
const THREADS = {
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
// <<< END inlined lib/schemas.js <<<

// --------------------------------------------------------------------------
// Tunables
// --------------------------------------------------------------------------

// Max topics dispatched per hop-level fan-out, to respect the concurrent-agent
// ceiling. Larger batches auto-queue across chunks (each chunk is a full
// search -> fetch-summarize -> decide pass; chunks run sequentially).
const CHUNK = 12;
// Auto-replan cycles the JS quality gate runs before it stops gating and just
// flags the still-low-confidence topics (the design's "no mid-run gate").
const MAX_AUTO_REPLANS = 2;

// --------------------------------------------------------------------------
// Agent dispatch — folds per-call data into the prompt (agent() has no input).
// --------------------------------------------------------------------------

function callAgent(prompt, opts = {}) {
  const { input, ...rest } = opts;
  const full =
    input === undefined
      ? prompt
      : `${prompt}\n\n## Input\n\`\`\`json\n${JSON.stringify(input, null, 2)}\n\`\`\`\n`;
  return agent(full, rest);
}

// researcher:search-agent — per-topic web search + T1-T4 source scoring.
function searchAgent(topic, config, hop) {
  return callAgent(
    'Run a web search for the topic in the Input and return selected_urls with tier/credibility scoring, following your agent instructions.',
    {
      agentType: 'researcher:search-agent',
      schema: SEARCH,
      label: `search:${topic.topic}@h${hop}`,
      phase: 'Research',
      input: {
        topic: topic.topic,
        depth: topic.depth,
        hop,
        seen_urls: topic.seen_urls ?? [],
        next_hop: topic.replan_hint ? null : topic.next_hop ?? null,
        replan_hint: topic.replan_hint ?? null,
        vault_root: config.vault_root ?? null,
        repo: config.repo ?? null,
      },
    },
  );
}

// researcher:fetch-summarize-runner — drives the Python fetch/summarize scripts.
function fetchSummarizeAgent(topic, hits, config, hop) {
  return callAgent(
    'Fetch and summarize the selected_urls in the Input by running the researcher Python scripts, following your agent instructions.',
    {
      agentType: 'researcher:fetch-summarize-runner',
      schema: SUMMARIES,
      label: `fetch-summarize:${topic.topic}@h${hop}`,
      phase: 'Research',
      input: {
        topic: topic.topic,
        depth: topic.depth,
        hop,
        selected_urls: hits?.selected_urls ?? [],
        config: {
          repo: config.repo ?? null,
          ollama_model: config.ollama_model ?? null,
          tier: config.tier ?? 'base',
          work_dir: config.work_dir ?? null,
        },
      },
    },
  );
}

// researcher:hop-planner — judgment-only next-hop pick (the JS loop owns the
// continue/stop/replan decision; we read only next_hop from its return).
function hopPlannerAgent(topic, vaultDigest, hop, config) {
  return callAgent(
    'Judge the best next hop for the topic in the Input, following your agent instructions.',
    {
      agentType: 'researcher:hop-planner',
      schema: HOP_NEXT,
      label: `hop-planner:${topic.topic}@h${hop}`,
      phase: 'Research',
      input: {
        topic: topic.topic,
        depth: topic.depth,
        current_hop: hop,
        summaries_so_far: topic.summaries ?? [],
        sources_so_far: topic.sources ?? [],
        hop_genealogy: topic.hop_genealogy ?? [],
        seen_urls: topic.seen_urls ?? [],
        vaultDigest: vaultDigest ?? [],
        vault_root: config?.vault_root ?? null,
        repo: config?.repo ?? null,
      },
    },
  );
}

// librarian:classify-agent — summaries -> neutral note-creation contract.
function classifyAgent(summaries, config) {
  return callAgent(
    'Classify these research summaries into the neutral note-creation contract, following your agent instructions.',
    {
      agentType: 'librarian:classify-agent',
      schema: CLASSIFY,
      label: 'classify',
      phase: 'Write',
      input: {
        summaries: summaries ?? [],
        vault_root: config.vault_root ?? null,
        vaultDigest: config.vaultDigest ?? [],
      },
    },
  );
}

// Librarian writer — a skill-using agent (NOT an agentType: the librarian
// note-write is the `librarian` skill, confirmed by the Task-0 spike). It owns
// write + wikilink + the SEQUENTIAL MOC update so the workflow body never races
// on shared MOC state.
function writerAgent(notes, vaultContext, contradictions, config, lowConfidence) {
  return callAgent(
    'Use the librarian skill to write these findings notes to the vault. For each entry in notes_to_create (the neutral input contract), author and write the note, add [[wikilinks]], and update the relevant MOC/index note — serialize MOC writes, never write the same MOC file in parallel. Then return ONLY a JSON object: {"written_notes":[{"path":"...","title":"...","action":"create|update"}],"updated_notes":["path", ...]}.',
    {
      schema: WRITE_RESULT,
      label: 'librarian-write',
      phase: 'Write',
      input: {
        notes_to_create: notes,
        vault_context: vaultContext,
        contradictions,
        low_confidence: lowConfidence ?? [],
        vault_root: config.vault_root ?? null,
        output_mode: config.outputMode ?? 'vault',
      },
    },
  );
}

// researcher:thread-discoverer — follow-up lead scoring for the thread gate.
function threadAgent(ctx) {
  return callAgent(
    'Discover follow-up research threads from the Input, following your agent instructions.',
    {
      agentType: 'researcher:thread-discoverer',
      schema: THREADS,
      label: 'thread-discoverer',
      phase: 'Discover',
      input: {
        project: ctx.project ?? null,
        summaries: ctx.summaries ?? [],
        written_notes: ctx.written_notes ?? [],
        vaultDigest: ctx.config?.vaultDigest ?? [],
        vault_root: ctx.config?.vault_root ?? null,
        repo: ctx.config?.repo ?? null,
      },
    },
  );
}

// --------------------------------------------------------------------------
// Pure helpers (no agent() calls) — ported from research-workflow's state.py +
// SKILL.md hop loop. The Workflow journal is the recovery layer, so the atomic
// state machine collapses to straight mutation.
// --------------------------------------------------------------------------

function initTopic(t) {
  const depth = t.depth && DEPTH_PROFILES[t.depth] ? t.depth : 'standard';
  return {
    topic: t.topic,
    depth,
    mode: t.mode ?? 'web_research',
    status: 'active',
    current_hop: 0,
    max_hops: getDepthProfile(depth).max_hops,
    next_hop: null,
    replan_hint: null,
    sources: [],
    summaries: [],
    seen_urls: [],
    contradictions: [],
    confidence_history: [],
    hop_genealogy: [],
  };
}

function chunk(arr, size) {
  if (!Array.isArray(arr)) return [];
  if (!Number.isFinite(size) || size <= 0) return arr.length ? [arr.slice()] : [];
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

function activeTopics(topics) {
  if (!Array.isArray(topics)) return [];
  return topics.filter((t) => t.status === 'active' && (t.current_hop ?? 0) < t.max_hops);
}

// Fold one topic-hop's search + summarize results into the topic's running
// state: append summaries, append confidence-bearing sources (deduped by URL),
// extend seen_urls. Tolerant of null agent results (keep-going .filter).
function ingestHopResults(topic, searchResult, summaryResult) {
  if (!Array.isArray(topic.summaries)) topic.summaries = [];
  if (!Array.isArray(topic.sources)) topic.sources = [];
  if (!Array.isArray(topic.seen_urls)) topic.seen_urls = [];
  const seen = new Set(topic.seen_urls);
  const haveSource = new Set(topic.sources.map((s) => s.url));
  for (const item of summaryResult?.items ?? []) {
    if (!item) continue;
    topic.summaries.push(item);
    if (item.url && !seen.has(item.url)) { topic.seen_urls.push(item.url); seen.add(item.url); }
    const ok = item.fetch_status ? item.fetch_status === 'ok' : true;
    if (ok && item.url && !haveSource.has(item.url)) {
      topic.sources.push({ url: item.url, tier: item.tier, is_primary: item.is_primary, credibility_score: item.credibility_score });
      haveSource.add(item.url);
    }
  }
  for (const sel of searchResult?.selected_urls ?? []) {
    if (sel?.url && !seen.has(sel.url)) { topic.seen_urls.push(sel.url); seen.add(sel.url); }
  }
  if (!Array.isArray(topic.manual_urls)) topic.manual_urls = [];
  const haveManual = new Set(topic.manual_urls.map((m) => m.url));
  for (const m of summaryResult?.manual_urls ?? []) {
    if (m?.url && !haveManual.has(m.url)) { topic.manual_urls.push({ topic: topic.topic, url: m.url, reason: m.reason ?? null }); haveManual.add(m.url); }
  }
  return topic;
}

// Apply one hop decision to a topic in place. Faithful port of
// state.apply_hop_decision (the JS loop computes the decision; the planner only
// supplies next_hop on a continue).
function applyDecision(topic, decision, conf, nextHop) {
  if (!Array.isArray(topic.hop_genealogy)) topic.hop_genealogy = [];
  if (!Array.isArray(topic.confidence_history)) topic.confidence_history = [];
  const hopData = {
    hop: (topic.current_hop ?? 0) + 1,
    confidence: conf.confidence,
    contradiction_rate: conf.contradictionRate,
    next_hop: decision === 'continue' ? nextHop ?? null : null,
    decision,
  };
  topic.hop_genealogy.push(hopData);
  topic.current_hop = (topic.current_hop ?? 0) + 1;
  topic.confidence_history.push(conf.confidence);
  topic.contradiction_rate = conf.contradictionRate;
  switch (decision) {
    case 'continue':
      topic.next_hop = nextHop ?? null;
      topic.replan_hint = null;
      break;
    case 'stop':
      topic.next_hop = null;
      topic.status = 'complete';
      break;
    case 'replan':
      topic.next_hop = null;
      topic.replan_hint = nextHop ?? null;
      topic.status = 'replan_pending';
      break;
    default:
      topic.next_hop = null;
      topic.status = 'complete';
  }
  return topic;
}

// Per-topic quality gate: latest confidence >= depth target AND contradiction
// rate <= 0.3. A topic with no history scores 0.
function topicPasses(topic) {
  const target = getDepthProfile(topic.depth).confidence_target;
  const history = Array.isArray(topic.confidence_history) ? topic.confidence_history : [];
  const latest = history.length ? history[history.length - 1] : 0;
  return latest >= target && (topic.contradiction_rate ?? 0) <= 0.3;
}

function failingTopics(topics) {
  if (!Array.isArray(topics)) return [];
  return topics.filter((t) => !topicPasses(t));
}

// Re-admit failing topics for one more replan pass: set the replan hint, bump
// max_hops (so a budget-exhausted topic is active again), mark active.
function readmitForReplan(failing) {
  for (const t of failing) {
    t.replan_hint = t.replan_hint ?? {
      issue: 'thin sources or low confidence',
      suggested_pattern: 'entity_expansion',
      suggested_query_focus: 'official / primary sources',
    };
    t.max_hops = (t.max_hops ?? 0) + 1;
    t.status = 'active';
  }
  return failing;
}

// Flatten every topic's summary items into one list for the write phase.
function collectSummaries(topics) {
  if (!Array.isArray(topics)) return [];
  const out = [];
  for (const t of topics) {
    for (const item of t.summaries ?? []) {
      if (item) out.push({ topic: t.topic, ...item });
    }
  }
  return out;
}

// Flatten every topic's manual_urls, deduped by URL across topics.
function collectManualUrls(topics) {
  const seen = new Set();
  const out = [];
  for (const t of Array.isArray(topics) ? topics : []) {
    for (const m of t.manual_urls ?? []) {
      if (m?.url && !seen.has(m.url)) { seen.add(m.url); out.push(m); }
    }
  }
  return out;
}

// --------------------------------------------------------------------------
// Research phase — multi-hop loop + JS quality gate (<=2 auto-replans).
// --------------------------------------------------------------------------

async function runResearch(topics, config) {
  const vaultDigest = config?.vaultDigest ?? [];
  let replanCount = 0;

  for (;;) {
    while (activeTopics(topics).length) {
      let allZero = true;
      for (const batch of chunk(activeTopics(topics), CHUNK)) {
        const hopOf = (t) => (t.current_hop ?? 0) + 1;

        const hits = await parallel(batch.map((t) => () => searchAgent(t, config, hopOf(t))));
        const sums = await parallel(batch.map((t, i) => () => fetchSummarizeAgent(t, hits[i], config, hopOf(t))));

        for (let i = 0; i < batch.length; i++) {
          const t = batch[i];
          const hop = hopOf(t);
          ingestHopResults(t, hits[i], sums[i]);
          if ((t.sources?.length ?? 0) > 0) allZero = false;

          const profile = getDepthProfile(t.depth);
          const confidence = computeConfidence(t.sources ?? [], t.depth);
          const cr = contradictionRate(t.sources ?? [], t.contradictions ?? []);
          const decision = decide({
            confidence,
            contradictionRate: cr,
            hop,
            maxHops: t.max_hops ?? profile.max_hops,
            target: profile.confidence_target,
          });
          const planned = decision === 'continue' ? await hopPlannerAgent(t, vaultDigest, hop, config) : null;
          applyDecision(t, decision, { confidence, contradictionRate: cr }, planned?.next_hop ?? null);
        }
      }

      if (allZero) {
        log?.('research-batch: every active topic fetched zero sources this hop — aborting.');
        return { aborted: true, reason: 'no_sources', manual_urls: collectManualUrls(topics), topics };
      }
    }

    const failing = failingTopics(topics);
    if (!failing.length) {
      log?.('research-batch: quality gate passed — all topics met their confidence target.');
      return { summaries: collectSummaries(topics), manual_urls: collectManualUrls(topics), topics, low_confidence: [] };
    }
    if (replanCount < MAX_AUTO_REPLANS) {
      replanCount += 1;
      log?.(`research-batch: ${failing.length} topic(s) below target — auto-replan ${replanCount}/${MAX_AUTO_REPLANS}.`);
      readmitForReplan(failing);
      continue;
    }
    // No mid-run gate: flag the still-low topics and proceed to write.
    log?.(`research-batch: ${failing.length} topic(s) still low-confidence after ${replanCount} replans — flagging (not gating).`);
    return { summaries: collectSummaries(topics), manual_urls: collectManualUrls(topics), topics, low_confidence: failing.map((t) => t.topic) };
  }
}

// --------------------------------------------------------------------------
// Write phase — Librarian classify -> skill-write (+ MOC + wikilink).
// --------------------------------------------------------------------------

async function runWrite(summaries, config, lowConfidence) {
  if (!summaries.length) {
    return { written_notes: [], updated_notes: [], reason: 'no_summaries' };
  }
  log?.('research-batch: classifying summaries via librarian.');
  const classification = await classifyAgent(summaries, config);
  const notes = classification?.notes_to_create ?? [];
  const contradictions = classification?.contradictions_detected ?? [];
  if (!notes.length) {
    return { written_notes: [], updated_notes: [], reason: 'no_notes', contradictions: contradictions.length };
  }
  log?.(`research-batch: librarian writing ${notes.length} note(s).`);
  const result = await writerAgent(
    notes,
    classification?.vault_context ?? {},
    contradictions,
    config,
    lowConfidence,
  );
  return {
    written_notes: (result?.written_notes ?? []).filter(Boolean),
    updated_notes: (result?.updated_notes ?? []).filter(Boolean),
    contradictions: contradictions.length,
  };
}

// --------------------------------------------------------------------------
// Entry — research -> write -> discover.
// --------------------------------------------------------------------------

async function run(args) {
  const plan = args.plan ?? {};
  const config = args.config ?? {};

  const topics = (plan.topics ?? [])
    .filter((t) => (t.mode ?? 'web_research') === 'web_research')
    .map((t) => initTopic(t));

  if (!topics.length) {
    return { error: 'no_web_research_topics', written_notes: [], updated_notes: [], threads: [], summary: { topics: 0 } };
  }
  log?.(`research-batch: starting ${topics.length} web_research topic(s).`);

  const research = await runResearch(topics, config);
  if (research.aborted) {
    return {
      aborted: true,
      reason: research.reason,
      written_notes: [],
      updated_notes: [],
      threads: [],
      manual_urls: research.manual_urls ?? [],
      summary: { project: plan.project ?? null, topics: topics.length, reason: research.reason },
    };
  }

  const write = await runWrite(research.summaries, config, research.low_confidence);

  log?.('research-batch: discovering follow-up threads.');
  const threadResult = await threadAgent({
    project: plan.project,
    summaries: research.summaries,
    written_notes: write.written_notes,
    config,
  });
  const threads = (threadResult?.threads ?? []).filter(Boolean);

  return {
    written_notes: write.written_notes,
    updated_notes: write.updated_notes,
    threads,
    manual_urls: research.manual_urls ?? [],
    summary: {
      project: plan.project ?? null,
      topics: topics.length,
      notes_written: write.written_notes.length,
      notes_updated: write.updated_notes.length,
      threads_found: threads.length,
      low_confidence: research.low_confidence ?? [],
      contradictions: write.contradictions ?? 0,
    },
  };
}

// ----- Workflow entry — the runtime executes this top-level body, not a
// default export. args may arrive as a JSON string; normalize before dispatch.
return await run(typeof args === 'string' ? JSON.parse(args) : args);
