import { readFileSync } from "node:fs";
import { join } from "node:path";
import { parse } from "yaml";
import { encode } from "../../src/msgpack.js";
import { repositoryRoot } from "../../src/shipped.js";
import type { Call, Row } from "../../src/binding.js";
import { Jeff } from "../../src/client.js";
import type { Decision } from "../../src/decision.js";
import type { BaseFormat, Engine, EngineResult } from "../../src/engine.js";
import { type CodeFunction, type NodeResult, graphFromDict, resultToJSON } from "../../src/graph.js";
import { type Policy, type Question, escalate, functionStep, human, llm } from "../../src/escalation.js";
import { type DecisionCase, type Predict, engineProbabilities } from "./decisions.js";
import { catalogueFor, normalised, toBase64, wireOf } from "./fixtures.js";

/** The fixtures' scripted engine: the n-th predict answers the n-th recorded predict, and must be asked for exactly
 * its adapter and rows (compared as MessagePack bytes). */
export class ScriptedEngine implements Engine {
  readonly mismatches: string[] = [];
  private readonly script: Predict[];

  constructor(private readonly served: string[], readonly baseFormat: BaseFormat, predicts: Predict[]) {
    this.script = [...predicts];
  }

  async installed(): Promise<Set<string>> {
    return new Set(this.served);
  }

  async predict(adapterId: string, rows: Row[]): Promise<EngineResult> {
    const next = this.script.shift();
    if (next === undefined) throw new Error(`The script has no answer left for a predict of ${adapterId}`);
    if (next.adapter !== adapterId) this.mismatches.push(`asked ${adapterId}, recorded ${next.adapter}`);
    if (toBase64(encode(rows)) !== next.rows_msgpack) this.mismatches.push(`rows of ${adapterId} differ from the recording`);
    return { probabilities: engineProbabilities(next), timing: next.timing, inputTokens: null };
  }

  remaining(): number {
    return this.script.length;
  }
}

/** What a recorded question is compared with: the question without its entries, which the fixture does not record
 * (their order is checked against the recorded question separately: entryOrderProblems). */
function recordable(question: Question) {
  const { stateEntries: _state, optionEntries: _options, ...recorded } = question;
  return recorded;
}

function asker(step: "llm" | "human", answer: string, questions: unknown[], asked: Question[]) {
  return (question: Question): string => {
    asked.push(question);
    questions.push({ step, question: recordable(question) });
    return answer;
  };
}

let inOrder: Map<string, any[]> | null = null;

/** Each case's recorded questions with every object a Map in the file's order. JSON.parse lists keys that look like
 * whole numbers (an option "0", a field "10") first, so the order is read again here with the YAML parser (JSON is
 * YAML), which keeps it. */
function recordedQuestionsInOrder(caseName: string): any[] {
  if (inOrder === null) {
    const text = readFileSync(join(repositoryRoot(), "contract", "conformance", "decisions.json"), "utf8");
    const parsed = parse(text, { mapAsMap: true }) as Map<string, any>;
    inOrder = new Map((parsed.get("cases") as Array<Map<string, any>>).map((item) => [item.get("name"), item.get("questions")]));
  }
  const questions = inOrder.get(caseName);
  if (questions === undefined) throw new Error(`decisions.json has no case ${caseName}`);
  return questions;
}

/** How the entries of the questions asked differ in order from the recorded questions (empty when they match); the
 * count and contents are compared with the questions themselves. */
export function entryOrderProblems(asked: readonly Question[], caseName: string): string[] {
  const recorded = recordedQuestionsInOrder(caseName);
  const problems: string[] = [];
  asked.slice(0, recorded.length).forEach((question, index) => {
    const expected = (recorded[index] as Map<string, any>).get("question") as Map<string, any>;
    const optionKeys = question.optionEntries.map(([key]) => key);
    const expectedOptions = [...(expected.get("options") as Map<string, unknown>).keys()];
    if (JSON.stringify(optionKeys) !== JSON.stringify(expectedOptions)) {
      problems.push(`question ${index}: options in order ${optionKeys.join(", ")}, recorded ${expectedOptions.join(", ")}`);
    }
    const state = expected.get("state");
    const stateKeys = question.stateEntries === null ? null : question.stateEntries.map(([key]) => key);
    const expectedState = typeof state === "string" ? null : [...(state as Map<string, unknown>).keys()];
    if (JSON.stringify(stateKeys) !== JSON.stringify(expectedState)) {
      problems.push(`question ${index}: state fields in order ${String(stateKeys)}, recorded ${String(expectedState)}`);
    }
  });
  return problems;
}

/** A fixture policy as a Node policy; any other string is passed on, for Jeff to refuse. */
export function policyFrom(spec: any, questions: unknown[], asked: Question[]): Policy {
  if (typeof spec === "string") return spec as Policy;
  const steps = spec.escalate.map((step: any) => {
    if ("function" in step) return functionStep(step.function);
    if ("llm" in step) return llm(asker("llm", step.llm, questions, asked));
    if ("human" in step) return human(asker("human", step.human, questions, asked));
    throw new Error(`Unknown escalation step ${JSON.stringify(step)}`);
  });
  return escalate(steps, spec.budget_ms === undefined ? {} : { budgetMs: spec.budget_ms });
}

/** Interface name -> Jeff method name. */
export const SHORTHANDS: Record<string, string> = {
  classify: "classify", route: "route", rate: "rate", detect: "detect", is_supported: "isSupported",
  equivalent: "equivalent", requires_escalation: "requiresEscalation",
};

const json = (decisions: Decision[]) => decisions.map((decision) => decision.toJSON());

export async function runOp(jeff: Jeff, op: string, args: any, questions: unknown[], asked: Question[]): Promise<unknown> {
  const policy = policyFrom(args.policy === undefined ? "return" : args.policy, questions, asked);
  if (op === "decide") return json(await jeff.decide(args.calls as Call[]));
  if (op === "function") {
    const bound = await jeff.function(args.ref, { context: args.context, labels: args.labels, onLowConfidence: policy });
    return json(await bound.many(args.inputs));
  }
  if (op === "shorthand") {
    const method = SHORTHANDS[args.interface] as "classify";
    return (await jeff[method](args.input, { function: args.function, context: args.context, labels: args.labels,
      onLowConfidence: policy })).toJSON();
  }
  if (op === "dispatch") {
    return (await jeff.dispatch(args.input, { interface: args.interface, context: args.context, labels: args.labels,
      router: args.router, onLowConfidence: policy })).toJSON();
  }
  if (op === "graph") {
    const codeCalls: unknown[] = [];
    const code: Record<string, CodeFunction> = Object.fromEntries(Object.entries((args.code ?? {}) as Record<string, unknown>)
      .map(([name, value]) => [name, (call: { input: unknown; params: unknown; nodes: Record<string, NodeResult> }) => {
        codeCalls.push({ code: name, input: call.input, params: call.params,
          nodes: Object.fromEntries(Object.entries(call.nodes).map(([node, result]) => [node, resultToJSON(result)])) });
        return structuredClone(value) as any;
      }]));
    const policies = Object.fromEntries(Object.entries((args.policies ?? {}) as Record<string, unknown>)
      .map(([node, spec]) => [node, policyFrom(spec, questions, asked)]));
    const graph = graphFromDict(structuredClone(args.graph), "the conformance graph");
    const result = await jeff.runGraph(graph, args.input, args.params, code, policies);
    return { graph_result: result.toJSON(), code_calls: codeCalls };
  }
  throw new Error(`runOp does not know op ${op}`);
}

export async function replayDecisionCase(item: DecisionCase) {
  const engine = new ScriptedEngine(item.installed, item.base_format, item.predicts);
  const questions: unknown[] = [];
  const asked: Question[] = [];
  let got: unknown;
  try {
    const jeff = await Jeff.create(engine, catalogueFor(item.catalogue));
    got = { result: normalised(await runOp(jeff, item.op, item.args, questions, asked)) };
  } catch (error) {
    got = { error: wireOf(error) };
  }
  return { got, engine, questions, entryOrder: entryOrderProblems(asked, item.name) };
}
