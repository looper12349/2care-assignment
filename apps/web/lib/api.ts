export class ApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); }
}

export async function api<T>(path: string, options: { method?: "GET" | "POST" | "PATCH"; body?: unknown; token?: string; signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  if (options.token) headers.authorization = `Bearer ${options.token}`;
  if (options.body !== undefined) headers["content-type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(path, { method: options.method || "GET", headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal, cache: "no-store" });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError("The local service could not be reached. Check that the services are running.", 0);
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(typeof result.detail === "string" ? result.detail : result.error || result.message || `Request failed (${response.status}).`, response.status);
  return result as T;
}

export const errorText = (error: unknown) => error instanceof Error ? error.message : "The request could not be completed.";
