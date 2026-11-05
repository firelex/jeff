import fs from 'node:fs';
import { parse } from 'yaml';
import type { Benchmark } from './benchmarks.js';

const FIELDS = ['id', 'name', 'rows', 'what', 'measures'] as const;

function benchmarkOf(raw: unknown, where: string): Benchmark {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) throw new Error(`${where}: must be a map of ${FIELDS.join(', ')}`);
  const entry = raw as Record<string, unknown>;
  const unknown = Object.keys(entry).filter((key) => !(FIELDS as readonly string[]).includes(key));
  if (unknown.length > 0) throw new Error(`${where}: unknown fields ${unknown.join(', ')}`);
  for (const key of ['id', 'name', 'what', 'measures'] as const) {
    if (typeof entry[key] !== 'string' || (entry[key] as string).trim() === '') throw new Error(`${where}: ${key} must be a non-empty string`);
  }
  if (!/^[a-z0-9-]+$/.test(entry.id as string)) throw new Error(`${where}: id ${String(entry.id)} must be lower-case letters, digits and hyphens`);
  if (typeof entry.rows !== 'number' || !Number.isInteger(entry.rows) || entry.rows <= 0) throw new Error(`${where}: rows must be a whole number above 0`);
  return {
    id: entry.id as string, name: entry.name as string, rows: entry.rows,
    what: (entry.what as string).trim(), measures: (entry.measures as string).trim(),
  };
}

// The benchmark sets of content/benchmarks.yaml, by id, in file order; an id used twice is an error.
export function loadBenchmarks(file: string): Map<string, Benchmark> {
  if (!fs.existsSync(file)) throw new Error(`No benchmarks file ${file}`);
  let data: unknown;
  try {
    data = parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    throw new Error(`${file}: not valid YAML: ${(error as Error).message}`, { cause: error });
  }
  if (!Array.isArray(data)) throw new Error(`${file}: must be a list of benchmark sets`);
  const out = new Map<string, Benchmark>();
  data.forEach((raw, i) => {
    const benchmark = benchmarkOf(raw, `${file}[${i}]`);
    if (out.has(benchmark.id)) throw new Error(`${file}: benchmark id ${benchmark.id} is used twice`);
    out.set(benchmark.id, benchmark);
  });
  return out;
}
