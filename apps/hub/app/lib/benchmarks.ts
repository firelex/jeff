// A benchmark set the docs' results tables name (content/benchmarks.yaml), as the browser shows it when the set's
// name is clicked (components/docs/BenchmarkTerm.tsx). No runtime code from a .server file, so the browser bundle can
// import it.
export interface Benchmark {
  id: string;
  // The set's name as the tables print it.
  name: string;
  rows: number;
  // What the set is: where its rows come from.
  what: string;
  // What a score on it measures.
  measures: string;
}

// A page links a set's name to its description with [name](#benchmark-<id>) (docs.server.ts checks every id).
export const BENCHMARK_LINK_PREFIX = '#benchmark-';
