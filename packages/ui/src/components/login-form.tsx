import { useId, useState, type FormEvent } from 'react';
import { Alert } from './alert';
import { Button } from './button';
import { Field } from './field';
import { Input } from './input';

export interface TypedError {
  type: string;
  message: string;
}

export interface LoginFormProps {
  onSubmit: (login: string, password: string) => void | Promise<void>;
  /** The server's typed error from the last failed attempt, or none. */
  error?: TypedError | null;
}

/**
 * Login/password sign-in. A login is a plain name such as "mathias", not necessarily an email address, so the
 * field is plain text. Owns its own fields and submitting state; a failed attempt is reported by the caller
 * through `error`, shown verbatim in an Alert.
 */
export function LoginForm({ onSubmit, error }: LoginFormProps) {
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const loginId = useId();
  const passwordId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await onSubmit(login, password);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex w-full max-w-sm flex-col gap-4">
      <Field label="Login" htmlFor={loginId}>
        <Input
          id={loginId}
          type="text"
          autoComplete="username"
          required
          value={login}
          onChange={(event) => setLogin(event.target.value)}
        />
      </Field>
      <Field label="Password" htmlFor={passwordId}>
        <Input
          id={passwordId}
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </Field>
      {error && (
        <Alert tone="error" title={error.type}>
          {error.message}
        </Alert>
      )}
      <Button type="submit" pending={submitting}>
        Sign in
      </Button>
    </form>
  );
}
