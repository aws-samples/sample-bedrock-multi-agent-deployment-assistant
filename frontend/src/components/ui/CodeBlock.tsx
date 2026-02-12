"use client";

import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";

interface CodeBlockProps {
  code: string;
  language?: string;
}

export function CodeBlock({ code, language = "hcl" }: CodeBlockProps) {
  return (
    <div className="rounded-lg overflow-hidden border border-gray-200">
      <SyntaxHighlighter
        language={language}
        style={oneLight}
        customStyle={{ margin: 0, fontSize: "0.8125rem" }}
        showLineNumbers
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}
