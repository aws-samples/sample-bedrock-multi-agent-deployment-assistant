"use client";

import { use, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { useWizardState } from "@/hooks/useWizardState";
import { PhaseIndicator } from "@/components/dashboard/PhaseIndicator";
import { RequirementsForm } from "@/components/wizard/RequirementsForm";
import { InterviewChat } from "@/components/wizard/InterviewChat";
import { DesignReview } from "@/components/wizard/DesignReview";
import { DeploymentParametersForm } from "@/components/wizard/DeploymentParametersForm";
import { IaCView } from "@/components/wizard/IaCView";
import { DocumentationView } from "@/components/wizard/DocumentationView";
import { DesignLoading } from "@/components/wizard/DesignLoading";
import { WizardStepBoundary } from "@/components/wizard/WizardStepBoundary";

export default function ProjectWorkspace({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const { tenantId: authTenantId } = useAuth();

  const onDesignSubmitted = useCallback(() => {
    router.push("/?designing=" + encodeURIComponent(id));
  }, [router, id]);

  const onIaCSubmitted = useCallback(() => {
    router.push("/?generating_iac=" + encodeURIComponent(id));
  }, [router, id]);

  const {
    step,
    requirementsSeed,
    requirements,
    showInterviewChat,
    recommendation,
    refinementPlan,
    designTaskId,
    designTaskStatus,
    iac,
    iacTaskId,
    iacTaskStatus,
    docs,
    docsTaskId,
    docsTaskStatus,
    regeneratingSection,
    loading,
    error,
    hydrating,
    tenantId,
    wsConnected,
    submitRequirements,
    interviewComplete,
    proceedToDesign,
    approveDesign,
    submitDeploymentParams,
    requestRedesign,
    regenerateIaC,
    generateDocs,
    regenerateSection,
    goBack,
    reset,
    handleIaCComplete,
    handleIaCFailed,
  } = useWizardState(id, authTenantId, { onDesignSubmitted, onIaCSubmitted });

  if (hydrating) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="text-center">
          <div className="inline-block h-8 w-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600" />
          <p className="mt-3 text-sm text-gray-500">Loading project...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex items-center justify-between">
            <div>
              <Link
                href="/"
                className="text-sm text-gray-500 hover:text-gray-700"
              >
                &larr; Dashboard
              </Link>
              <h1 className="text-xl font-bold text-gray-900 mt-1">
                Project: {id}
              </h1>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <PhaseIndicator currentStep={step} />

        <WizardStepBoundary stepName="Requirements" onReset={reset}>
          {/* Seed form: shown when interview chat is not yet active */}
          {step === "requirements" && !showInterviewChat && !loading && (
            <RequirementsForm
              onSubmit={submitRequirements}
              loading={loading}
              error={error}
            />
          )}

          {/* Design loading: shown when transitioning from interview to design */}
          {step === "requirements" && !showInterviewChat && loading && requirements && (
            <DesignLoading
              useCaseSummary={`${(requirements.use_cases || []).join(", ")} deployment`}
              taskStatus={designTaskStatus}
              taskId={designTaskId}
            />
          )}

          {/* Interview chat: always shown after seed submission */}
          {step === "requirements" && showInterviewChat && requirementsSeed && (
            <InterviewChat
              seed={requirementsSeed}
              onComplete={interviewComplete}
              onProceedToDesign={proceedToDesign}
              tenantId={tenantId}
              projectId={id}
            />
          )}
        </WizardStepBoundary>

        <WizardStepBoundary stepName="Design Review" onReset={reset}>
          {/* Design options: shown when recommendation is available and no refinement plan yet */}
          {step === "design" && recommendation && !refinementPlan && (
            <DesignReview
              recommendation={recommendation}
              onApprove={approveDesign}
              onRequestRedesign={requestRedesign}
              onBack={goBack}
              loading={loading}
              error={error}
            />
          )}

          {/* Deployment parameters form: shown after design selection returns a refinement plan */}
          {step === "design" && refinementPlan && (
            <DeploymentParametersForm
              refinementPlan={refinementPlan}
              projectName={id}
              onSubmit={submitDeploymentParams}
              onBack={goBack}
              loading={loading}
              error={error}
            />
          )}
        </WizardStepBoundary>

        <WizardStepBoundary stepName="Infrastructure as Code" onReset={reset}>
          {step === "iac" && (
            <IaCView
              iac={iac}
              iacTaskId={iacTaskId}
              iacTaskStatus={iacTaskStatus}
              onIaCComplete={handleIaCComplete}
              onIaCFailed={handleIaCFailed}
              onRegenerateIaC={regenerateIaC}
              onContinue={generateDocs}
              onBack={goBack}
              loading={loading}
              error={error}
              projectId={id}
              tenantId={tenantId}
              wsConnected={wsConnected}
            />
          )}
        </WizardStepBoundary>

        <WizardStepBoundary stepName="Documentation" onReset={reset}>
          {step === "documentation" && (
            <DocumentationView
              docs={docs}
              docsTaskId={docsTaskId}
              docsTaskStatus={docsTaskStatus}
              onBack={goBack}
              onReset={reset}
              onRegenerateSection={regenerateSection}
              regeneratingSection={regeneratingSection}
              loading={loading}
              error={error}
            />
          )}
        </WizardStepBoundary>
      </main>
    </div>
  );
}
