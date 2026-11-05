// The catalogue's topics: the adapters grouped by what they decide, so the list reads as a map of what Jeff can do.
// A topic holds one domain (the second part of an adapter's id, jeff.<domain>.<name>), or a domain with a single
// adapter merged into its obvious neighbour (knowledge into reasoning). The order puts the general building blocks
// most applications use first, then agents and message handling, then technical areas, then the industry-specific
// ones. Only the public catalogue's domains have a topic (the hidden adapters are never grouped). No .server code here: the catalogue page groups in the browser, after filtering.
import type { CatalogueEntry } from './catalogue.server';

export interface Topic {
  id: string;
  domains: string[];
  heading: string;
  description: string;
}

export const TOPICS: Topic[] = [
  {
    id: 'reasoning', domains: ['reasoning', 'knowledge'], heading: 'Reasoning and knowledge',
    description: 'Whether evidence supports a claim, two statements conflict or mean the same, an item meets a requirement or a pack is complete; and general-knowledge questions.',
  },
  {
    id: 'retrieval', domains: ['retrieval'], heading: 'Search and documents',
    description: 'Which source a query goes to, whether a passage or a set of passages answers it, what a document or section is, and whether an extracted value is right.',
  },
  {
    id: 'agents', domains: ['agents'], heading: 'AI agents',
    description: 'The checks around each step of an agent: which tool to call, whether an action may run, whether a result helps, what to do next, whether the task is done, and whether a reply needs careful thinking.',
  },
  {
    id: 'routing', domains: ['routing'], heading: 'Routing',
    description: 'Which tool an agent calls next, or which specialist adapter answers a request.',
  },
  {
    id: 'support', domains: ['support'], heading: 'Support and inboxes',
    description: 'What an incoming message or ticket asks for, who should handle it, how urgent it is and whether a team must act.',
  },
  {
    id: 'safety', domains: ['safety'], heading: 'Safety',
    description: 'Prompt injection and jailbreak attempts, spam and phishing in what users and outside sources send.',
  },
  {
    id: 'language', domains: ['language'], heading: 'Language',
    description: 'The feeling expressed in a short comment or message.',
  },
  {
    id: 'security', domains: ['security'], heading: 'Security operations',
    description: 'What a security team should do about a new alert under its playbook.',
  },
  {
    id: 'engineering', domains: ['engineering'], heading: 'Software engineering',
    description: 'What kind of failure a broken build is, and which part of a project owns a new issue or pull request.',
  },
  {
    id: 'compliance', domains: ['compliance'], heading: 'Compliance',
    description: "Applying a firm's rules: sanctions screening of names.",
  },
  {
    id: 'legal', domains: ['legal'], heading: 'Legal',
    description: 'What type a contract clause is.',
  },
  {
    id: 'finance', domains: ['finance'], heading: 'Trading',
    description: "What a trading desk's rules say to do with the order book.",
  },
];

const TOPIC_OF_DOMAIN = new Map(TOPICS.flatMap((topic) => topic.domains.map((domain) => [domain, topic] as const)));

function topicOf(entry: CatalogueEntry): Topic {
  const topic = TOPIC_OF_DOMAIN.get(entry.domain);
  if (topic === undefined) {
    throw new Error(`${entry.id}: no catalogue topic covers its domain ${entry.domain} (add it to TOPICS in app/lib/topics.ts)`);
  }
  return topic;
}

// `entries` under their topics, in TOPICS order; a topic with no entries is left out. Inside a topic, open adapters
// come first, then any others (the tests' made-up entries), each alphabetical by title.
export function groupByTopic(entries: CatalogueEntry[]): { topic: Topic; entries: CatalogueEntry[] }[] {
  const byTopic = new Map<string, CatalogueEntry[]>();
  for (const entry of entries) {
    const { id } = topicOf(entry);
    const list = byTopic.get(id);
    if (list === undefined) byTopic.set(id, [entry]);
    else list.push(entry);
  }
  const order = (a: CatalogueEntry, b: CatalogueEntry) =>
    (a.kind === b.kind ? 0 : a.kind === 'open' ? -1 : 1) || a.title.localeCompare(b.title, 'en');
  return TOPICS.flatMap((topic) => {
    const list = byTopic.get(topic.id);
    return list === undefined ? [] : [{ topic, entries: [...list].sort(order) }];
  });
}
