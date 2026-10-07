import { NextRequest, NextResponse } from "next/server";
import { isSameOrigin } from "@/lib/origin";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ path: string[] }> };

/** Thin transport adapter. The Python service owns validation and authorization. */
async function forward(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  if (path.some((part) => !/^[a-zA-Z0-9_-]+$/.test(part))) {
    return NextResponse.json({ detail: "Invalid API path." }, { status: 400 });
  }
  const origin = request.headers.get("origin");
  if (request.method !== "GET" && !isSameOrigin(origin, request.headers.get("host"), request.nextUrl.protocol)) {
    return NextResponse.json({ detail: "Cross-origin changes are not allowed." }, { status: 403 });
  }
  const base = process.env.CONVERSATION_URL || "http://127.0.0.1:8001";
  const destination = new URL(`/api/${path.join("/")}`, base);
  destination.search = request.nextUrl.search;
  const headers = new Headers({ accept: "application/json" });
  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("authorization", authorization);
  let body: string | undefined;
  if (request.method !== "GET") {
    body = await request.text();
    if (Buffer.byteLength(body, "utf8") > 16_384) {
      return NextResponse.json({ detail: "Request is too large." }, { status: 413 });
    }
    if (body) headers.set("content-type", "application/json");
  }
  try {
    const response = await fetch(destination, {
      method: request.method,
      headers,
      body: body || undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(90_000),
      redirect: "error",
    });
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { "content-type": response.headers.get("content-type") || "application/json", "cache-control": "no-store" },
    });
  } catch {
    return NextResponse.json({ detail: "The conversation service could not be reached. Check the local services and refresh the conversation before retrying a booking." }, { status: 502 });
  }
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
