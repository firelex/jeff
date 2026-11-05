// The two contract schemas of the internal API jeff-server polls (contract/schemas/accounts_keys.schema.json and
// accounts_usage.schema.json), copied here because the hub's function is bundled from this package's own files only
// (scripts/check-build.mjs refuses an import from outside apps/hub). test/internal-schemas.test.ts fails when a copy
// differs from its contract file: change the contract first, then copy it here.
export const ACCOUNTS_KEYS_SCHEMA = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://jeff.dev/schemas/accounts_keys.schema.json",
  "title": "GET /api/internal/v1/keys response",
  "description": "The hub's list of every API key that may be used now, sent to jeff-server once a minute. accounts: every account whose subscription may use keys (Stripe status active, trialing or past_due), with its allowance in input tokens and its current billing period. keys: the SHA-256 hash (lowercase hex) of every live key of those accounts, with its account. playground: the hub playground's own key, which has no allowance and is limited to runs_per_second across all visitors; its period is the current calendar month in UTC, for counting only. Times are UTC, to the second, with a Z.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "accounts",
    "keys",
    "playground"
  ],
  "$defs": {
    "account_id": {
      "type": "string",
      "pattern": "^[A-Za-z0-9_-]{1,64}$"
    },
    "hash": {
      "type": "string",
      "pattern": "^[0-9a-f]{64}$"
    },
    "time": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
    }
  },
  "properties": {
    "accounts": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "account",
          "allowance",
          "period_start",
          "period_end"
        ],
        "properties": {
          "account": {
            "$ref": "#/$defs/account_id"
          },
          "allowance": {
            "type": "integer",
            "minimum": 1
          },
          "period_start": {
            "$ref": "#/$defs/time"
          },
          "period_end": {
            "$ref": "#/$defs/time"
          }
        }
      }
    },
    "keys": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "hash",
          "account"
        ],
        "properties": {
          "hash": {
            "$ref": "#/$defs/hash"
          },
          "account": {
            "$ref": "#/$defs/account_id"
          }
        }
      }
    },
    "playground": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "hash",
        "runs_per_second",
        "period_start",
        "period_end"
      ],
      "properties": {
        "hash": {
          "$ref": "#/$defs/hash"
        },
        "runs_per_second": {
          "type": "number",
          "exclusiveMinimum": 0
        },
        "period_start": {
          "$ref": "#/$defs/time"
        },
        "period_end": {
          "$ref": "#/$defs/time"
        }
      }
    }
  }
};

export const ACCOUNTS_USAGE_SCHEMA = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://jeff.dev/schemas/accounts_usage.schema.json",
  "title": "POST /api/internal/v1/usage request",
  "description": "jeff-server's usage report, sent to the hub once a minute: totals, never deltas, so a repeated or lost report does no harm. accounts: each account's input tokens counted so far in a billing period (the current one and the one before), by the period's start; the playground's usage is reported under the account id 'playground'. keys: when each key was last used. Times are UTC, to the second, with a Z.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "accounts",
    "keys"
  ],
  "$defs": {
    "time": {
      "type": "string",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
    }
  },
  "properties": {
    "accounts": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "account",
          "period_start",
          "input_tokens"
        ],
        "properties": {
          "account": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,64}$"
          },
          "period_start": {
            "$ref": "#/$defs/time"
          },
          "input_tokens": {
            "type": "integer",
            "minimum": 0
          }
        }
      }
    },
    "keys": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "hash",
          "last_used"
        ],
        "properties": {
          "hash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "last_used": {
            "$ref": "#/$defs/time"
          }
        }
      }
    }
  }
};
