"use client";

import { Component, ReactNode } from "react";

interface Props {
  stepName: string;
  children: ReactNode;
  onReset?: () => void;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Error boundary that wraps individual wizard steps.
 * Catches render errors within a step and shows a recovery UI
 * instead of crashing the entire page.
 */
export class WizardStepBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(`[WizardStepBoundary] Error in "${this.props.stepName}":`, error, info.componentStack);
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  handleReset = () => {
    this.setState({ hasError: false, error: null });
    this.props.onReset?.();
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="bg-white rounded-lg border border-red-200 shadow-sm">
          <div className="px-6 py-4 border-b border-red-100">
            <h2 className="text-lg font-semibold text-red-800">
              Error in {this.props.stepName}
            </h2>
          </div>
          <div className="px-6 py-5 text-center">
            <p className="text-sm text-gray-600 mb-1">
              Something went wrong rendering this step.
            </p>
            <p className="text-xs text-gray-400 mb-4 font-mono">
              {this.state.error?.message}
            </p>
            <div className="flex justify-center gap-3">
              <button
                onClick={this.handleRetry}
                className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
              >
                Try again
              </button>
              {this.props.onReset && (
                <button
                  onClick={this.handleReset}
                  className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
                >
                  Reset step
                </button>
              )}
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
