/** Every error Jeff raises (contract/errors.yaml): one class per entry, all JeffError subclasses. `details` holds the
 * error's wire details exactly as the Python SDK sends them (snake_case keys), so an error crosses the engine socket,
 * HTTP and the channel unchanged; the typed getters read them. */

import { Decision, type DecisionJSON } from "./decision.js";
import { pyFloat, pyRepr } from "./pyformat.js";

export type ErrorDetails = Record<string, unknown>;

/** A required string field of an error's wire details: throws naming the error type and field instead of coercing
 * a missing or wrongly-typed value into the text "undefined". (Called only from a typed getter, after module load
 * has finished defining every error class below, so referencing ContractError here ahead of its declaration is
 * safe: function bodies are not evaluated until they are called.) */
function requiredString(details: ErrorDetails, key: string, errorType: string): string {
  const value = details[key];
  if (typeof value !== "string") throw new ContractError(`${errorType}.details is missing its '${key}' string (got ${pyRepr(value)})`);
  return value;
}

/** As requiredString, for a number field (Busy/RateLimited's retry_after_s). */
function requiredNumber(details: ErrorDetails, key: string, errorType: string): number {
  const value = details[key];
  if (typeof value !== "number") throw new ContractError(`${errorType}.details is missing its '${key}' number (got ${pyRepr(value)})`);
  return value;
}

/** As requiredString, for an array-of-strings field (MissingContext's fields). */
function requiredStringArray(details: ErrorDetails, key: string, errorType: string): string[] {
  const value = details[key];
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new ContractError(`${errorType}.details is missing its '${key}' string array (got ${pyRepr(value)})`);
  }
  return [...(value as string[])];
}

export class JeffError extends Error {
  static readonly errorType: string = "JeffError";
  readonly details: ErrorDetails;
  /** The x-request-id of the Jeff server answer that carried this error (set by the remote client), else null. */
  requestId: string | null = null;
  /** Further failures noticed while handling this one (Python's add_note); not part of the message. */
  readonly notes: string[] = [];

  constructor(message: string, details: ErrorDetails = {}) {
    super(message);
    this.details = { ...details };
    Object.defineProperty(this, "name", {
      value: (new.target as typeof JeffError).errorType, enumerable: false, configurable: true, writable: true,
    });
  }

  get type(): string {
    return this.name;
  }
}

export type ErrorClass = new (message: string, details?: ErrorDetails) => JeffError;

export class ContractError extends JeffError { static override readonly errorType = "ContractError"; }
export class ManifestError extends JeffError { static override readonly errorType = "ManifestError"; }
export class InvalidReference extends JeffError { static override readonly errorType = "InvalidReference"; }
export class UnknownFunction extends JeffError { static override readonly errorType = "UnknownFunction"; }
export class NotInstalled extends JeffError {
  static override readonly errorType = "NotInstalled";
  get functionId(): string { return requiredString(this.details, "function_id", this.type); }
}
export class MissingContext extends JeffError {
  static override readonly errorType = "MissingContext";
  get functionId(): string { return requiredString(this.details, "function_id", this.type); }
  get fields(): string[] { return requiredStringArray(this.details, "fields", this.type); }
}
export class UnknownContext extends JeffError { static override readonly errorType = "UnknownContext"; }
export class MissingLabels extends JeffError {
  static override readonly errorType = "MissingLabels";
  get functionId(): string { return requiredString(this.details, "function_id", this.type); }
}
export class InvalidLabels extends JeffError { static override readonly errorType = "InvalidLabels"; }
export class TooManyLabels extends JeffError { static override readonly errorType = "TooManyLabels"; }
export class TooManyHandles extends JeffError { static override readonly errorType = "TooManyHandles"; }
export class InterfaceMismatch extends JeffError { static override readonly errorType = "InterfaceMismatch"; }
export class LowConfidence extends JeffError {
  static override readonly errorType = "LowConfidence";
  /** The decision that abstained, as its wire details carry it. */
  get decision(): Decision {
    const data = this.details.decision;
    if (data === null || typeof data !== "object") {
      throw new ContractError(`${this.type}.details is missing its 'decision' object (got ${pyRepr(data)})`);
    }
    return Decision.fromJSON(data as DecisionJSON);
  }
}
export class EscalationFailed extends JeffError {
  static override readonly errorType = "EscalationFailed";
  get step(): string { return requiredString(this.details, "step", this.type); }
}
export class EscalationBudgetExceeded extends JeffError { static override readonly errorType = "EscalationBudgetExceeded"; }
export class InvalidPolicy extends JeffError { static override readonly errorType = "InvalidPolicy"; }
export class GraphError extends JeffError { static override readonly errorType = "GraphError"; }
export class RuntimeMissing extends JeffError { static override readonly errorType = "RuntimeMissing"; }
export class ProprietaryWeights extends JeffError {
  static override readonly errorType = "ProprietaryWeights";
  get adapterId(): string { return requiredString(this.details, "adapter_id", this.type); }
}
export class CalibrationError extends JeffError { static override readonly errorType = "CalibrationError"; }
export class EngineError extends JeffError { static override readonly errorType = "EngineError"; }
export class InputTooLong extends JeffError { static override readonly errorType = "InputTooLong"; }
export class BackendUnavailable extends JeffError { static override readonly errorType = "BackendUnavailable"; }
export class Busy extends JeffError {
  static override readonly errorType = "Busy";
  get retryAfterS(): number { return requiredNumber(this.details, "retry_after_s", this.type); }
}
export class DeadlineExceeded extends JeffError { static override readonly errorType = "DeadlineExceeded"; }
export class ProtocolMismatch extends JeffError { static override readonly errorType = "ProtocolMismatch"; }
export class NotReady extends JeffError { static override readonly errorType = "NotReady"; }
export class Unauthorised extends JeffError { static override readonly errorType = "Unauthorised"; }
export class NotLicensed extends JeffError { static override readonly errorType = "NotLicensed"; }
export class RateLimited extends JeffError {
  static override readonly errorType = "RateLimited";
  get retryAfterS(): number { return requiredNumber(this.details, "retry_after_s", this.type); }
}
export class UsageLimitReached extends JeffError {
  static override readonly errorType = "UsageLimitReached";
  get account(): string { return requiredString(this.details, "account", this.type); }
  get used(): number { return requiredNumber(this.details, "used", this.type); }
  get allowance(): number { return requiredNumber(this.details, "allowance", this.type); }
  get periodEnd(): string { return requiredString(this.details, "period_end", this.type); }
}
export class InvalidRequest extends JeffError { static override readonly errorType = "InvalidRequest"; }
export class Conflict extends JeffError { static override readonly errorType = "Conflict"; }
export class FactoryOff extends JeffError { static override readonly errorType = "FactoryOff"; }

const CLASSES: ReadonlyArray<ErrorClass & { errorType: string }> = [
  JeffError, ContractError, ManifestError, InvalidReference, UnknownFunction, NotInstalled, MissingContext,
  UnknownContext, MissingLabels, InvalidLabels, TooManyLabels, TooManyHandles, InterfaceMismatch, LowConfidence,
  EscalationFailed, EscalationBudgetExceeded, InvalidPolicy, GraphError, RuntimeMissing, ProprietaryWeights,
  CalibrationError, EngineError, InputTooLong, BackendUnavailable, Busy, DeadlineExceeded, ProtocolMismatch,
  NotReady, Unauthorised, NotLicensed, RateLimited, UsageLimitReached, InvalidRequest, Conflict, FactoryOff,
];

export const ERROR_CLASSES: ReadonlyMap<string, ErrorClass> = new Map(CLASSES.map((cls) => [cls.errorType, cls]));

export function notInstalled(functionId: string, why = ""): NotInstalled {
  const dots = functionId.split(".").length - 1;
  const adapter = dots >= 3 ? functionId.slice(0, functionId.lastIndexOf(".")) : functionId;
  return new NotInstalled(`${functionId} is not installed${why ? `: ${why}` : ""}. Install it with: jeff pull ${adapter}`,
    { function_id: functionId });
}

export function lowConfidence(decision: Decision): LowConfidence {
  const confidence = decision.confidence === null ? "None" : pyFloat(decision.confidence);
  return new LowConfidence(`${decision.function} abstained (confidence ${confidence}); on_low_confidence='error'`,
    { decision: decision.toJSON() });
}

export function missingContext(functionId: string, fields: string[]): MissingContext {
  return new MissingContext(`${functionId} needs context field(s) ${fields.join(", ")}; pass them in context=`,
    { function_id: functionId, fields: [...fields] });
}

export function missingLabels(functionId: string): MissingLabels {
  return new MissingLabels(`${functionId} takes its labels from the caller; pass labels=`, { function_id: functionId });
}

export function escalationFailed(step: string, reason: string): EscalationFailed {
  return new EscalationFailed(`Escalation step ${step} failed: ${reason}`, { step });
}

export function errorFromWire(body: { type: unknown; message: unknown; details?: unknown }): JeffError {
  const name = String(body.type);
  const message = String(body.message);
  const ErrorClass = ERROR_CLASSES.get(name);
  if (ErrorClass === undefined) return new EngineError(`unknown error type ${pyRepr(name)} from the engine: ${message}`);
  const details = body.details !== null && typeof body.details === "object" ? body.details as ErrorDetails : {};
  return new ErrorClass(message, details);
}

export function errorToWire(error: JeffError): { type: string; message: string; details: ErrorDetails } {
  return { type: error.type, message: error.message, details: { ...error.details } };
}

/** The same error (class, message, details, notes) as a new instance, for each waiting caller to get its own. */
export function copyError(error: JeffError): JeffError {
  const copy = errorFromWire(errorToWire(error));
  copy.notes.push(...error.notes);
  copy.requestId = error.requestId;
  return copy;
}
