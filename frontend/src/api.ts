// Serialize requests because each authenticated response rotates the session cookie.
// A stale request from another tab fails closed; the user can reload that tab.
let queue: Promise<unknown> = Promise.resolve();
export function api(path: string, init: RequestInit = {}): Promise<Response> {
  const result = queue.then(() => fetch(path, { ...init, credentials: 'include' }));
  queue = result.catch(() => undefined);
  return result;
}
