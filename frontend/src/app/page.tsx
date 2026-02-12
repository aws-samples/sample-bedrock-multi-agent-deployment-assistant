"use client";

import { Suspense, useEffect, useState, useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { Project } from "@/lib/types";
import { listProjects, createProject, deleteProject } from "@/lib/api";

const STEP_LABELS: Record<string, string> = {
  requirements: "Requirements",
  design: "Design Review",
  iac: "IaC Generation",
  documentation: "Documentation",
  complete: "Complete",
};

export default function DashboardPage() {
  return (
    <Suspense>
      <Dashboard />
    </Suspense>
  );
}

function Dashboard() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Show notification when redirected from design submission
  const designingProjectId = searchParams.get("designing");
  const [showDesignNotification, setShowDesignNotification] = useState(!!designingProjectId);

  // Show notification when redirected from IaC submission
  const generatingIaCProjectId = searchParams.get("generating_iac");
  const [showIaCNotification, setShowIaCNotification] = useState(!!generatingIaCProjectId);

  // Auto-refresh projects while any project has an active design or IaC task
  const hasActiveTasks = projects.some((p) => !!p.active_design_task_id || !!p.active_iac_task_id);

  useEffect(() => {
    if (!hasActiveTasks) return;

    const id = setInterval(async () => {
      try {
        const fresh = await listProjects();
        setProjects(fresh);

        // Dismiss the notification once the triggering project's task completes
        if (designingProjectId) {
          const target = fresh.find((p) => p.project_id === designingProjectId);
          if (target && !target.active_design_task_id) {
            setShowDesignNotification(false);
          }
        }

        if (generatingIaCProjectId) {
          const target = fresh.find((p) => p.project_id === generatingIaCProjectId);
          if (target && !target.active_iac_task_id) {
            setShowIaCNotification(false);
          }
        }
      } catch {
        // Silently ignore — the user already sees the stale list
      }
    }, 5_000);

    return () => clearInterval(id);
  }, [hasActiveTasks, designingProjectId, generatingIaCProjectId]);

  const fetchProjects = useCallback(async () => {
    try {
      setError(null);
      const data = await listProjects();
      setProjects(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Operation failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  const handleCreate = async () => {
    const trimmedName = projectName.trim();
    if (!trimmedName) return;

    setCreating(true);
    setError(null);

    try {
      const project = await createProject(trimmedName);
      router.push(`/project/${project.project_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Operation failed");
      setCreating(false);
    }
  };

  const handleDelete = async (projectId: string, projectName: string) => {
    const confirmed = window.confirm(`Delete "${projectName}"? This cannot be undone.`);
    if (!confirmed) return;

    try {
      await deleteProject(projectId);
      setProjects((prev) => prev.filter((p) => p.project_id !== projectId));
    } catch {
      setError("Failed to delete project");
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-gray-900">AI-LCM</h1>
              <p className="text-sm text-gray-500">
                FortiGate Deployment Assistant
              </p>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Projects</h2>
            <p className="text-sm text-gray-500">
              Design and deploy FortiGate solutions on AWS with AI-powered
              guidance.
            </p>
          </div>
          {!showForm && (
            <button
              onClick={() => setShowForm(true)}
              className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
            >
              + New Project
            </button>
          )}
        </div>

        {showForm && (
          <div className="mb-6 p-4 bg-white rounded-lg border border-gray-200">
            <div className="flex items-end gap-3">
              <div className="flex-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Project Name
                </label>
                <input
                  type="text"
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleCreate()}
                  placeholder="e.g., Production SD-WAN Deployment"
                  className="w-full px-3 py-2 bg-white text-gray-900 border border-gray-300 rounded-lg text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 placeholder:text-gray-400"
                  autoFocus
                />
              </div>
              <button
                onClick={handleCreate}
                disabled={creating || !projectName.trim()}
                className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {creating ? "Creating..." : "Create"}
              </button>
              <button
                onClick={() => {
                  setShowForm(false);
                  setProjectName("");
                }}
                className="px-4 py-2 text-sm font-medium text-gray-600 hover:text-gray-800 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {showDesignNotification && (
          <div className="mb-4 p-3 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-700 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="h-4 w-4 rounded-full border-2 border-blue-500 border-t-transparent animate-spin shrink-0" />
              <span>
                Design is being generated. You&apos;ll see the results when you open the project.
              </span>
            </div>
            <button
              onClick={() => setShowDesignNotification(false)}
              className="text-blue-400 hover:text-blue-600 ml-4 shrink-0"
              aria-label="Dismiss"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}

        {showIaCNotification && (
          <div className="mb-4 p-3 bg-indigo-50 border border-indigo-200 rounded-lg text-sm text-indigo-700 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="h-4 w-4 rounded-full border-2 border-indigo-500 border-t-transparent animate-spin shrink-0" />
              <span>
                Infrastructure as Code is being generated. You&apos;ll see the results when you open the project.
              </span>
            </div>
            <button
              onClick={() => setShowIaCNotification(false)}
              className="text-indigo-400 hover:text-indigo-600 ml-4 shrink-0"
              aria-label="Dismiss"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        )}

        {error && (
          <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
            {error}
          </div>
        )}

        {loading ? (
          <div className="py-12 text-center">
            <div className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600" />
            <p className="mt-2 text-sm text-gray-500">Loading projects...</p>
          </div>
        ) : projects.length === 0 ? (
          <div className="py-12 text-center bg-white rounded-lg border border-gray-200">
            <p className="text-gray-500">
              No projects yet. Create your first project to get started.
            </p>
          </div>
        ) : (
          <div className="grid gap-3">
            {projects.map((project) => (
              <div
                key={project.project_id}
                className="flex items-center justify-between p-4 bg-white rounded-lg border border-gray-200 hover:border-gray-300 transition-colors"
              >
                <button
                  onClick={() => router.push(`/project/${project.project_id}`)}
                  className="flex-1 text-left"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex-1">
                      <h3 className="text-sm font-medium text-gray-900">
                        {project.name}
                      </h3>
                      <div className="flex items-center gap-2 mt-1">
                        {project.active_iac_task_id ? (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-indigo-50 text-indigo-700">
                            <div className="h-3 w-3 rounded-full border-2 border-indigo-500 border-t-transparent animate-spin" />
                            Generating IaC…
                          </span>
                        ) : project.active_design_task_id ? (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-amber-50 text-amber-700">
                            <div className="h-3 w-3 rounded-full border-2 border-amber-500 border-t-transparent animate-spin" />
                            Generating Design…
                          </span>
                        ) : (
                          <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-blue-50 text-blue-700">
                            {STEP_LABELS[project.current_step] ?? project.current_step}
                          </span>
                        )}
                        <span className="text-xs text-gray-400">
                          Updated {new Date(project.updated_at).toLocaleDateString()}
                        </span>
                      </div>
                    </div>
                    <span className="text-gray-400 text-sm">&rarr;</span>
                  </div>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(project.project_id, project.name);
                  }}
                  className="ml-4 p-1 text-gray-400 hover:text-red-500 transition-colors"
                  aria-label={`Delete project ${project.name}`}
                  title="Delete project"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
