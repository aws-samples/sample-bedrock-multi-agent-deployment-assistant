"use client";

import { useState, useRef, useEffect } from "react";
import type { RequirementsSeed, InterviewOutput, InputHint } from "@/lib/types";
import { invokeInterviewChat } from "@/lib/api";
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

function formatOptionLabel(value: string): string {
  return value
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

interface InterviewChatProps {
  seed: RequirementsSeed;
  onComplete: (requirements: InterviewOutput) => void;
  onProceedToDesign: () => void;
  tenantId?: string;
  projectId?: string;
}

export function InterviewChat({
  seed,
  onComplete,
  onProceedToDesign,
  tenantId = "default",
  projectId = "default",
}: InterviewChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [interviewComplete, setInterviewComplete] = useState(false);
  const [missingFields, setMissingFields] = useState<string[]>([]);
  const [gatheredCount, setGatheredCount] = useState(0);
  const [inputHint, setInputHint] = useState<InputHint | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const seedAsRequirements: Record<string, unknown> = {
    use_cases: seed.use_cases,
    bandwidth: seed.bandwidth,
    solution_description: seed.solution_description,
  };

  const useCaseStr = seed.use_cases.join(",");
  const [gatheredFields, setGatheredFields] = useState<Record<string, unknown>>(seedAsRequirements);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function handleResponse(response: { content: string; complete: boolean; requirements?: InterviewOutput; missingFields?: string[]; gatheredFields?: Record<string, unknown>; inputHint?: InputHint }) {
    const latestGathered = response.gatheredFields
      ? { ...gatheredFields, ...response.gatheredFields }
      : gatheredFields;
    if (response.gatheredFields) {
      setGatheredFields(latestGathered);
    }
    if (response.missingFields) {
      updateProgress(response.missingFields, Object.keys(latestGathered).length);
    }
    if (response.complete && response.requirements) {
      setInterviewComplete(true);
      onComplete(response.requirements);
    }
    setInputHint(response.inputHint ?? null);
  }

  useEffect(() => {
    const abortController = new AbortController();
    setLoading(true);

    invokeInterviewChat(
      "I've provided my basic requirements. Please review them and start the technical interview.",
      tenantId,
      projectId,
      undefined,
      useCaseStr,
      seedAsRequirements,
      abortController.signal,
      seedAsRequirements,
    )
      .then((response) => {
        if (abortController.signal.aborted) return;
        setMessages([{ role: "assistant", content: response.content }]);
        handleResponse(response);
      })
      .catch((err) => {
        if (abortController.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;

        setMessages([
          {
            role: "assistant",
            content: "Welcome! I'll help gather the technical requirements for your deployment. Let's start — what routing protocol are you planning to use?",
          },
        ]);
      })
      .finally(() => {
        if (!abortController.signal.aborted) {
          setLoading(false);
        }
      });

    return () => {
      abortController.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const totalFieldsRef = useRef(0);
  const [totalFields, setTotalFields] = useState(0);

  function updateProgress(missing: string[], knownGatheredCount: number) {
    setMissingFields(missing);

    // Total = gathered (seed + auto-filled + answered) + still missing
    const total = knownGatheredCount + missing.length;
    totalFieldsRef.current = total;
    setTotalFields(total);
    setGatheredCount(knownGatheredCount);
  }

  async function sendMessage(text: string) {
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInputHint(null);
    setLoading(true);

    try {
      const response = await invokeInterviewChat(
        text,
        tenantId,
        projectId,
        undefined,
        useCaseStr,
        undefined,
        undefined,
        gatheredFields,
      );

      setMessages((prev) => [...prev, { role: "assistant", content: response.content }]);
      handleResponse(response);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Sorry, I encountered an error. Please try sending your message again." },
      ]);
    } finally {
      setLoading(false);
    }
  }

  function handleSend() {
    if (!input.trim() || loading) return;
    const text = input.trim();
    setInput("");
    sendMessage(text);
  }

  function handleOptionSelect(value: string) {
    if (loading) return;
    sendMessage(value);
  }

  const progressPercent = totalFields > 0 ? Math.round((gatheredCount / totalFields) * 100) : 0;

  // Chat stays open while there are still questions to answer, even after blocking fields are done
  const chatFinished = interviewComplete && missingFields.length === 0;

  const useCaseLabel = seed.use_cases.map((uc) => uc.toUpperCase().replace("-", " ")).join(", ");

  return (
    <div className="mt-6 border border-gray-200 rounded-lg bg-white overflow-hidden flex flex-col" style={{ height: "calc(100vh - 280px)" }}>
      <div className="px-5 py-4 bg-gray-50 border-b border-gray-200 shrink-0">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-base font-semibold text-gray-900">
              AI Technical Interview
            </h3>
            <p className="text-sm text-gray-500 mt-0.5">
              The assistant will gather detailed requirements for your {useCaseLabel} deployment.
            </p>
          </div>
          {interviewComplete && (
            <button
              onClick={onProceedToDesign}
              className="px-4 py-2 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
            >
              Proceed to Design
            </button>
          )}
        </div>

        <div className="mt-3">
          {totalFields > 0 ? (
            <>
              <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
                <span>
                  {gatheredCount} of {totalFields} fields gathered
                </span>
                <span>{progressPercent}%</span>
              </div>
              <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${
                    interviewComplete ? "bg-green-500" : "bg-blue-500"
                  }`}
                  style={{ width: `${progressPercent}%` }}
                />
              </div>
              {missingFields.length > 0 && !interviewComplete && (
                <p className="mt-1 text-xs text-gray-400 truncate">
                  Still needed: {missingFields.slice(0, 4).join(", ")}
                  {missingFields.length > 4 && ` +${missingFields.length - 4} more`}
                </p>
              )}
              {interviewComplete && missingFields.length > 0 && (
                <p className="mt-1 text-xs text-green-600">
                  Ready to proceed — {missingFields.length} optional {missingFields.length === 1 ? "field" : "fields"} remaining
                </p>
              )}
              {chatFinished && (
                <p className="mt-1 text-xs text-green-600 font-medium">
                  All requirements gathered. Ready to proceed to design.
                </p>
              )}
            </>
          ) : (
            <>
              <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
                <span>Analyzing requirements...</span>
              </div>
              <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden">
                <div className="h-full w-1/3 rounded-full bg-blue-400 animate-pulse" />
              </div>
            </>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-5 space-y-4">
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
          >
            {msg.role === "user" ? (
              <div className="max-w-[80%] px-4 py-2.5 rounded-2xl rounded-br-sm bg-blue-600 text-white text-sm">
                {msg.content}
              </div>
            ) : (
              <div className="max-w-[85%] px-4 py-3 rounded-2xl rounded-bl-sm bg-gray-100">
                <MarkdownRenderer content={msg.content} />
              </div>
            )}
          </div>
        ))}
        {inputHint?.options && !loading && !chatFinished && (
          <div className="flex justify-start">
            <div className="flex flex-wrap gap-2 max-w-[85%]">
              {inputHint.options.map((option) => (
                <button
                  key={option}
                  onClick={() => handleOptionSelect(option)}
                  className="px-3 py-1.5 text-sm bg-blue-50 text-blue-700 border border-blue-200 rounded-full hover:bg-blue-100 hover:border-blue-300 transition-colors cursor-pointer"
                >
                  {formatOptionLabel(option)}
                </button>
              ))}
            </div>
          </div>
        )}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-100 text-gray-500 px-4 py-3 rounded-2xl rounded-bl-sm text-sm">
              <span className="inline-flex gap-1">
                <span className="animate-bounce">.</span>
                <span className="animate-bounce [animation-delay:0.2s]">.</span>
                <span className="animate-bounce [animation-delay:0.4s]">.</span>
              </span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Input */}
      <div className="px-5 py-4 border-t border-gray-200 flex gap-3 shrink-0">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
          placeholder={chatFinished ? "Interview complete — proceed to design" : interviewComplete ? "Answer optional questions or proceed to design..." : "Type your response..."}
          disabled={loading || chatFinished}
          className="flex-1 px-4 py-2.5 bg-white text-gray-900 border border-gray-300 rounded-xl text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-gray-400 disabled:opacity-50"
        />
        <button
          onClick={handleSend}
          disabled={loading || chatFinished || !input.trim()}
          className="px-5 py-2.5 text-sm font-medium bg-blue-600 text-white rounded-xl hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          Send
        </button>
      </div>
    </div>
  );
}
