"use client";

import React from "react";
import { ErrorState } from "@/components/atlas";

interface WidgetBoundaryProps {
  children: React.ReactNode;
  widgetName?: string;
}

interface State {
  hasError: boolean;
}

export class WidgetBoundary extends React.Component<
  WidgetBoundaryProps,
  State
> {
  constructor(props: WidgetBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  override render() {
    if (this.state.hasError) {
      return (
        <ErrorState
          heading="Widget unavailable"
          subtext={`The ${this.props.widgetName ?? "widget"} encountered an error.`}
          onRetry={() => this.setState({ hasError: false })}
        />
      );
    }
    return this.props.children;
  }
}
