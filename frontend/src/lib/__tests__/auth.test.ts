import { describe, it, expect, beforeEach } from "vitest";
import {
  isLocalMode,
  getStoredToken,
  getStoredTenantId,
  storeAuth,
  clearAuth,
  parseTenantFromToken,
} from "@/lib/auth";

/* ------------------------------------------------------------------ */
/*  sessionStorage polyfill (jsdom provides one, but we clear it)      */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  sessionStorage.clear();
});

/* ------------------------------------------------------------------ */
/*  isLocalMode                                                        */
/* ------------------------------------------------------------------ */

describe("isLocalMode()", () => {
  it("returns true when env vars are empty (default)", () => {
    // In the test environment NEXT_PUBLIC_COGNITO_DOMAIN and
    // NEXT_PUBLIC_COGNITO_CLIENT_ID are not set, so the module-level
    // constants default to "".
    expect(isLocalMode()).toBe(true);
  });
});

/* ------------------------------------------------------------------ */
/*  getStoredToken / storeAuth / clearAuth                             */
/* ------------------------------------------------------------------ */

describe("token storage", () => {
  it("getStoredToken returns null when nothing stored", () => {
    expect(getStoredToken()).toBeNull();
  });

  it("storeAuth persists token and tenant, getStoredToken retrieves token", () => {
    storeAuth("my-token", "my-tenant");

    expect(getStoredToken()).toBe("my-token");
    expect(getStoredTenantId()).toBe("my-tenant");
  });

  it("clearAuth removes token and tenant", () => {
    storeAuth("tok", "ten");
    clearAuth();

    expect(getStoredToken()).toBeNull();
    expect(getStoredTenantId()).toBe("local-dev"); // falls back to default
  });

  it("getStoredTenantId returns local-dev when nothing stored", () => {
    expect(getStoredTenantId()).toBe("local-dev");
  });
});

/* ------------------------------------------------------------------ */
/*  parseTenantFromToken                                               */
/* ------------------------------------------------------------------ */

describe("parseTenantFromToken()", () => {
  function makeJwt(payload: Record<string, unknown>): string {
    const header = btoa(JSON.stringify({ alg: "RS256", typ: "JWT" }));
    const body = btoa(JSON.stringify(payload));
    return `${header}.${body}.fake-signature`;
  }

  it("extracts custom:tenant_id from a JWT payload", () => {
    const token = makeJwt({ "custom:tenant_id": "acme-corp", sub: "user1" });
    expect(parseTenantFromToken(token)).toBe("acme-corp");
  });

  it("falls back to sub when custom:tenant_id is missing", () => {
    const token = makeJwt({ sub: "user-abc" });
    expect(parseTenantFromToken(token)).toBe("user-abc");
  });

  it("returns 'default' when both custom:tenant_id and sub are missing", () => {
    const token = makeJwt({ email: "a@b.com" });
    expect(parseTenantFromToken(token)).toBe("default");
  });

  it("returns 'default' for a malformed token", () => {
    expect(parseTenantFromToken("not.a.jwt")).toBe("default");
  });

  it("returns 'default' for an empty string", () => {
    expect(parseTenantFromToken("")).toBe("default");
  });

  it("handles base64url-encoded payloads (with - and _)", () => {
    // Manually create a base64url-encoded payload
    const payload = { "custom:tenant_id": "tenant+special/chars" };
    const header = btoa(JSON.stringify({ alg: "RS256" }));
    // Use standard base64 then convert to base64url
    const bodyB64 = btoa(JSON.stringify(payload));
    const bodyB64url = bodyB64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const token = `${header}.${bodyB64url}.sig`;

    expect(parseTenantFromToken(token)).toBe("tenant+special/chars");
  });
});
