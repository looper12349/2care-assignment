/** Next.js can canonicalize nextUrl.hostname. Preserve the browser's Host header. */
export function isSameOrigin(origin: string | null, host: string | null, protocol: string): boolean {
  // Non-browser API clients use bearer authentication without an Origin header.
  if (!origin) return true;
  if (!host || !["http:", "https:"].includes(protocol)) return false;
  try {
    const requested = new URL(`${protocol}//${host}`);
    const supplied = new URL(origin);
    const plainOrigin = (url: URL) => !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash;
    return plainOrigin(requested) && plainOrigin(supplied) && supplied.origin === requested.origin;
  } catch {
    return false;
  }
}
