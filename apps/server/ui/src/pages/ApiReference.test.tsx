import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { ApiError, type Api } from '../api';
import { ApiReference } from './ApiReference';

// A small fixture OpenAPI document, standing in for GET /openapi.json: enough paths, schemas and named variant
// request/response schemas to check the page's rendering without depending on the real contract/openapi.yaml.
const FIXTURE_SPEC = {
  info: {
    title: 'Jeff HTTP API',
    version: '1.0.0',
    description:
      'An overview of the HTTP API: POST /v1/decide and friends. The low-latency channel, a WebSocket at /v1/channel, '
      + 'is not an HTTP operation and is not listed under paths. The MCP (Model Context Protocol) endpoint, /mcp, is '
      + 'not listed under paths either.',
  },
  paths: {
    '/v1/adapters': {
      get: {
        summary: 'List every catalogue adapter and whether it is served.',
        description: '500 EngineError while any engine is down.',
        responses: {
          '200': { description: 'The catalogue.', content: { 'application/json': { schema: { $ref: '#/components/schemas/adapters_response' } } } },
          '401': { description: 'HTTP 401; see contract/errors.yaml for which error type(s) answer with this status.' },
          '500': { description: 'HTTP 500; see contract/errors.yaml for which error type(s) answer with this status.' },
        },
      },
    },
    '/v1/adapters/served': {
      put: {
        summary: 'Replace what every engine serves.',
        description: 'Owner-only; refused on a public server.',
        requestBody: { required: true, content: { 'application/json': { schema: { $ref: '#/components/schemas/served_request' } } } },
        responses: {
          '200': { description: 'The adapters after the change.', content: { 'application/json': { schema: { $ref: '#/components/schemas/adapters_response' } } } },
          '403': { description: 'HTTP 403; see contract/errors.yaml for which error type(s) answer with this status.' },
          '422': { description: 'HTTP 422; see contract/errors.yaml for which error type(s) answer with this status.' },
        },
      },
    },
    // Exercises variant naming's three tiers in one place: the request's first variant is a named $ref
    // (CallsRequest, with its own title); the second is an inline schema with no $ref or title, named by its
    // first required property ("dispatch"). The response's two variants are both inline and unnamed, named by
    // "decisions" / "decision" respectively -- the same shape as the real decide_response.
    '/v1/decide': {
      post: {
        summary: 'Run the planner.',
        requestBody: {
          required: true,
          content: {
            'application/json': {
              schema: {
                oneOf: [
                  { $ref: '#/components/schemas/CallsRequest' },
                  { type: 'object', required: ['dispatch'], properties: { dispatch: { type: 'string' } } },
                ],
              },
            },
          },
        },
        responses: {
          '200': {
            description: 'One reply per request shape.',
            content: {
              'application/json': {
                schema: {
                  oneOf: [
                    { type: 'object', required: ['decisions'], properties: { decisions: { type: 'array', items: { type: 'string' } } } },
                    { type: 'object', required: ['decision'], properties: { decision: { type: 'string' } } },
                  ],
                },
              },
            },
          },
          '409': { description: 'HTTP 409; see contract/errors.yaml for which error type(s) answer with this status.' },
        },
      },
    },
  },
  components: {
    headers: {
      XRequestId: { description: 'A 32-character lowercase-hex request id, set on every response this server sends.' },
    },
    schemas: {
      CallsRequest: {
        title: 'CallsRequest',
        type: 'object',
        required: ['calls'],
        properties: { calls: { type: 'array', items: { type: 'string' } } },
      },
      served_request: {
        type: 'object',
        required: ['adapters'],
        properties: { adapters: { type: 'array', items: { type: 'string', minLength: 1 }, description: 'The adapters to serve.' } },
      },
      adapters_response: {
        type: 'object',
        required: ['adapters', 'adapters_version'],
        properties: {
          adapters: {
            type: 'array',
            items: {
              type: 'object',
              required: ['id', 'served'],
              properties: { id: { type: 'string', description: "The adapter's catalogue ID." }, served: { type: 'boolean' } },
            },
          },
          adapters_version: { type: 'integer' },
        },
      },
    },
  },
};

function fakeApi(overrides: Partial<Api> = {}): Api {
  return { openapi: vi.fn(async () => FIXTURE_SPEC), ...overrides } as unknown as Api;
}

describe('ApiReference', () => {
  it('renders every path and method, its request fields, response fields, and the error statuses with their error types', async () => {
    render(<ApiReference api={fakeApi()} />);
    // Each path/method shows twice: once in the anchor list at the top, once on its own route card.
    expect((await screen.findAllByText('/v1/adapters')).length).toBe(2);
    expect(screen.getAllByText('/v1/adapters/served').length).toBe(2);
    expect(screen.getByText('Replace what every engine serves.')).toBeTruthy();

    // Request fields of PUT /v1/adapters/served (served_request).
    expect(screen.getAllByText('adapters').length).toBeGreaterThan(0);
    expect(screen.getAllByText('list of string').length).toBeGreaterThan(0);
    expect(screen.getAllByText('required').length).toBeGreaterThan(0);

    // Response fields of GET /v1/adapters and PUT /v1/adapters/served (both answer adapters_response).
    expect(screen.getAllByText('adapters_version').length).toBe(2);
    expect(screen.getAllByText('integer').length).toBe(2);

    // The error statuses, each with the error type(s) that answer it (from contract/errors.yaml, bundled on the page).
    expect(screen.getByText('401')).toBeTruthy();
    expect(screen.getByText('Unauthorised')).toBeTruthy();
    expect(screen.getByText('403')).toBeTruthy();
    expect(screen.getByText('NotLicensed, FactoryOff')).toBeTruthy();
    expect(screen.getByText('422')).toBeTruthy();
    expect(screen.getByText(/InvalidRequest/)).toBeTruthy();
    expect(screen.getByText('500')).toBeTruthy();
    // "EngineError" also turns up in the route's own description (contract prose mentions error names inline, e.g.
    // "500 EngineError while any engine is down"); the error-type row is the one that lists it alongside its kin.
    expect(screen.getByText('JeffError, ContractError, ManifestError, EngineError')).toBeTruthy();
  });

  it('names oneOf variants by $ref, then title, then first required property -- never "object"', async () => {
    render(<ApiReference api={fakeApi()} />);
    await screen.findAllByText('/v1/decide');
    // Request: a named $ref (CallsRequest) and an inline variant with no $ref/title (named "dispatch" -- which
    // also happens to be that variant's own one property, so the name shows up twice: as the variant's own
    // heading and as that property's row, exactly as the real decide_request's "dispatch" variant does).
    expect(screen.getByText('CallsRequest')).toBeTruthy();
    expect(screen.getAllByText('dispatch').length).toBe(2);
    // Response: both variants are inline with no $ref/title, named by their own first required property --
    // which, like decide_response's real "decisions" and "decision" variants, is also that variant's one
    // property, so each name shows up twice (the variant heading and the property row).
    expect(screen.getAllByText('decisions').length).toBe(2);
    expect(screen.getAllByText('decision').length).toBe(2);
    expect(screen.queryByText('object')).toBeNull();
  });

  it('documents x-request-id from the document\'s own header description', async () => {
    render(<ApiReference api={fakeApi()} />);
    expect(await screen.findByText(/A 32-character lowercase-hex request id, set on every response this server sends\./)).toBeTruthy();
  });

  it('a list of every path and method sits at the top of the page', async () => {
    render(<ApiReference api={fakeApi()} />);
    const nav = await screen.findByRole('navigation', { name: 'Every path' });
    expect(within(nav).getAllByRole('button').length).toBe(3);
    expect(nav.querySelectorAll('a').length).toBe(0);
    expect(nav.textContent).toContain('/v1/adapters');
    expect(nav.textContent).toContain('/v1/decide');
  });

  it('clicking a path scrolls its route card into view and leaves the console route unchanged', async () => {
    window.location.hash = '#api';
    const scrolled: Element[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this);
    };
    try {
      render(<ApiReference api={fakeApi()} />);
      const nav = await screen.findByRole('navigation', { name: 'Every path' });
      fireEvent.click(within(nav).getByRole('button', { name: /POST \/v1\/decide/i }));
      expect(window.location.hash).toBe('#api');
      expect(scrolled.length).toBe(1);
      expect(scrolled[0].textContent).toContain('Run the planner.');
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it('a malformed /openapi.json shows the problem in an error alert, not a spinner forever', async () => {
    const openapi = vi.fn(async () => ({ paths: {} }));
    render(<ApiReference api={fakeApi({ openapi })} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('without a usable info.title');
    expect(screen.queryByText('Loading /openapi.json')).toBeNull();
  });

  it('shows a curl sample with a placeholder bearer token, never a real one, and a Python sample', async () => {
    render(<ApiReference api={fakeApi()} />);
    await screen.findAllByText('/v1/adapters/served');
    expect(screen.getByText(/curl -X PUT .*\/v1\/adapters\/served/)).toBeTruthy();
    const curlBlocks = screen.getAllByText(/Authorization: Bearer <token>/);
    expect(curlBlocks.length).toBeGreaterThan(0);
    expect(screen.getAllByText(/import requests/).length).toBeGreaterThan(0);
  });

  it('shows the channel and MCP from the OpenAPI description, as plain text', async () => {
    render(<ApiReference api={fakeApi()} />);
    expect(await screen.findByText('The channel and MCP')).toBeTruthy();
    expect(screen.getByText(/The low-latency channel, a WebSocket at \/v1\/channel/)).toBeTruthy();
    expect(screen.getByText(/The MCP \(Model Context Protocol\) endpoint, \/mcp/)).toBeTruthy();
  });

  it('a failed GET /openapi.json shows the server\'s typed error', async () => {
    const openapi = vi.fn(async () => {
      throw new ApiError({ status: 429, type: 'RateLimited', message: 'Too many requests', details: {}, requestId: 'req-429' });
    });
    render(<ApiReference api={fakeApi({ openapi })} />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('RateLimited: Too many requests');
  });
});
