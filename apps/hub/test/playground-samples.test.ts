// @vitest-environment node
import { expect, it } from 'vitest';
import { playgroundLanguages, playgroundSamples, SAMPLES_PER_ADAPTER, sampleById } from '../app/lib/playground-samples.server';
import { shownLanguages } from '../app/lib/site.server';

it('offers at most three live samples per adapter, from the hub example files', () => {
  const samples = playgroundSamples();
  expect(samples.length).toBeGreaterThan(0);
  const perAdapter = new Map<string, number>();
  for (const sample of samples) {
    expect(sample.example.offline).toBe(false);
    expect(sample.example.function.startsWith(`${sample.adapter}.`)).toBe(true);
    perAdapter.set(sample.adapter, (perAdapter.get(sample.adapter) ?? 0) + 1);
  }
  expect(Math.max(...perAdapter.values())).toBeLessThanOrEqual(SAMPLES_PER_ADAPTER);
  expect(sampleById(samples[0].id)?.id).toBe(samples[0].id);
  expect(sampleById('no-such-sample')).toBeNull();
});

it('shows code in the languages the pages show', () => {
  expect(playgroundLanguages()).toEqual(shownLanguages());
});
