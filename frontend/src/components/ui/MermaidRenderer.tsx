"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import DOMPurify from "dompurify";

interface MermaidRendererProps {
  code: string;
}

let mermaidInitialized = false;
let initPromise: Promise<void> | null = null;

async function ensureMermaidInit(): Promise<void> {
  if (mermaidInitialized) return;
  if (initPromise) return initPromise;

  initPromise = (async () => {
    const mermaid = (await import("mermaid")).default;

    // Fetch AWS icon pack from static asset
    const res = await fetch("/aws-icons-mermaid.json");
    const awsIcons = await res.json();

    mermaid.registerIconPacks([{ name: "aws", loader: () => awsIcons }]);
    mermaid.initialize({
      startOnLoad: false,
      theme: "neutral",
      securityLevel: "strict",
    });

    mermaidInitialized = true;
  })().catch((err) => {
    initPromise = null;
    throw err;
  });

  return initPromise;
}

export function MermaidRenderer({ code }: MermaidRendererProps) {
  const [svgHtml, setSvgHtml] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [showCode, setShowCode] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const renderIdRef = useRef(0);

  // Render diagram when code changes
  useEffect(() => {
    if (!code) return;

    const currentId = ++renderIdRef.current;

    async function render() {
      try {
        await ensureMermaidInit();
        const mermaid = (await import("mermaid")).default;

        // Mermaid requires unique IDs for each render call
        const { svg } = await mermaid.render(
          `mermaid-diagram-${currentId}`,
          code,
        );

        if (renderIdRef.current === currentId) {
          const cleanSvg = DOMPurify.sanitize(svg, {
            USE_PROFILES: { svg: true, svgFilters: true },
            ADD_TAGS: ["use"],
            FORBID_ATTR: ["xlink:href"],
          });
          setSvgHtml(cleanSvg);
          setError(null);
        }
      } catch (e) {
        if (renderIdRef.current === currentId) {
          setError(e instanceof Error ? e.message : "Diagram rendering failed");
        }
      }
    }

    render();
  }, [code]);

  const copyToClipboard = useCallback(
    (text: string, label: string) => {
      navigator.clipboard.writeText(text).then(() => {
        setCopied(label);
        setTimeout(() => setCopied(null), 2000);
      });
    },
    [],
  );

  if (!code) {
    return (
      <div className="py-12 text-center text-gray-400 text-sm">
        No architecture diagram available. Generate IaC first.
      </div>
    );
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex gap-2">
          <button
            onClick={() => setShowCode(false)}
            className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
              !showCode
                ? "bg-blue-100 text-blue-700"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            Diagram
          </button>
          <button
            onClick={() => setShowCode(true)}
            className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
              showCode
                ? "bg-blue-100 text-blue-700"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            Code
          </button>
        </div>
        <div className="flex gap-2">
          {!showCode && svgHtml && (
            <button
              onClick={() => copyToClipboard(svgHtml, "SVG")}
              className="px-3 py-1 text-xs text-gray-600 bg-gray-100 rounded-md hover:bg-gray-200"
            >
              {copied === "SVG" ? "Copied!" : "Copy SVG"}
            </button>
          )}
          {showCode && (
            <button
              onClick={() => copyToClipboard(code, "Code")}
              className="px-3 py-1 text-xs text-gray-600 bg-gray-100 rounded-md hover:bg-gray-200"
            >
              {copied === "Code" ? "Copied!" : "Copy Code"}
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      {showCode ? (
        <pre className="bg-gray-50 border border-gray-200 rounded-lg p-4 text-xs overflow-x-auto whitespace-pre font-mono">
          {code}
        </pre>
      ) : error ? (
        <div>
          <p className="text-sm text-amber-600 mb-3">
            Diagram rendering failed: {error}
          </p>
          <pre className="bg-gray-50 border border-gray-200 rounded-lg p-4 text-xs overflow-x-auto whitespace-pre font-mono">
            {code}
          </pre>
        </div>
      ) : svgHtml ? (
        <div
          className="bg-white border border-gray-200 rounded-lg p-4 overflow-auto"
          dangerouslySetInnerHTML={{ __html: svgHtml }}
        />
      ) : (
        <div className="py-12 text-center text-gray-400 text-sm">
          Rendering diagram...
        </div>
      )}
    </div>
  );
}
