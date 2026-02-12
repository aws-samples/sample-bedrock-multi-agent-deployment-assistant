export type UseCases = "sd-wan" | "egress" | "ingress" | "inspection" | "notknown";

export type RoutingProtocol = "bgp" | "static-route" | "notknown";

export type WorkloadResilience =
  | "none"
  | "ha-single-region-single-zone"
  | "ha-single-region-dual-zone"
  | "ha-dual-region-single-zone"
  | "ha-dual-region-dual-zone"
  | "notknown";

export interface UserInformation {
  name: string;
  experience_on_cloud: string;
}

export interface InterviewOutput {
  use_cases: UseCases[];
  cloud_routing_protocol: RoutingProtocol;
  resilience: WorkloadResilience;
  bandwidth: number;
  user_info?: UserInformation;
  compliance: string[];
  solution_description: string;
  use_case_details?: Record<string, Record<string, unknown>>;
}

export interface RequirementsSeed {
  use_cases: UseCases[];
  bandwidth: number;
  solution_description: string;
}

export interface VPCBlueprint {
  role: string;
  subnet_roles: string[];
  availability_zones: number;
}

export interface InterfaceBlueprint {
  port_name: string;
  subnet_role: string;
  description: string;
}

export interface FortiGateBlueprint {
  role: string;
  vpc_role: string;
  interfaces: InterfaceBlueprint[];
}

export interface KBReference {
  source_uri: string;
  excerpt: string;
  relevance_score: number;
}

export interface DesignOption {
  name: string;
  description: string;
  architecture_summary: string;
  pros: string[];
  cons: string[];
  estimated_monthly_cost_usd: number;
  security_posture_rating: number;
  complexity_rating: number;
  deployment_pattern: string;
  use_case: string;
  ha_mode: string;
  fortigate_instance_type: string;
  aws_services: string[];
  vpc_topology: VPCBlueprint[];
  fortigate_topology: FortiGateBlueprint[];
  has_code_template: boolean;
  template_s3_prefix: string | null;
  kb_references: KBReference[];
  well_architected_assessment: Record<string, string> | null;
}

export interface DesignRecommendation {
  options: DesignOption[];
  recommended_option_index: number;
  rationale: string;
  requirements_summary: string;
  available_templates: string[];
}

export interface RefinementField {
  field_name: string;
  label: string;
  description: string;
  required: boolean;
  default_value: string | null;
  default_rationale: string | null;
  input_type: "text" | "select" | "cidr" | "number";
  options: string[] | null;
  validation_pattern: string | null;
}

export interface RefinementPlan {
  fields: RefinementField[];
  kb_configuration_notes: string;
  template_parameters_found: string[];
  kb_references: KBReference[];
}

export interface DeploymentParameters {
  aws_region: string;
  vpc_cidr: string;
  environment: string;
  project_name: string;
  additional_parameters: Record<string, unknown>;
}

export interface DesignTaskResponse {
  task_id: string;
  status: "queued" | "processing" | "completed" | "failed";
  submitted_at?: string;
  result?: DesignRecommendation;
  error?: string;
}

export interface ValidationFinding {
  layer: string;
  severity: string;
  rule_id: string;
  message: string;
  resource?: string;
  line?: number;
  file?: string;
}

export interface ValidationReport {
  passed: boolean;
  findings: ValidationFinding[];
  fix_attempts: number;
  layers_executed: string[];
}

export interface IaCOutput {
  files: Record<string, string>;
  validation_report: ValidationReport;
  template_resolution_path: string;
  generation_duration_ms: number;
}

export type IaCTaskStatus = 'queued' | 'processing' | 'validating' | 'completed' | 'failed';

export interface IaCTaskResponse {
  task_id: string;
  status: IaCTaskStatus;
  submitted_at?: string;
  result?: IaCOutput;
  error?: string;
}

export interface DocumentationOutput {
  user_guide: string;
  threat_model: string;
  architecture_diagram: string;
  diagram_fix_attempts?: number;
  diagram_validation_passed?: boolean;
}

export type DocsTaskStatus = 'queued' | 'processing' | 'completed' | 'failed';

export interface DocsTaskResponse {
  task_id: string;
  status: DocsTaskStatus;
  submitted_at?: string;
  result?: DocumentationOutput;
  error?: string;
}

export interface RegenerateDocsSectionResponse {
  section: keyof DocumentationOutput;
  content: string;
}

export interface InputHint {
  field_path: string;
  type: string;
  options?: string[];
}

export type WizardStep = "requirements" | "design" | "iac" | "documentation";

export interface WizardState {
  step: WizardStep;
  requirementsSeed: RequirementsSeed | null;
  requirements: InterviewOutput | null;
  showInterviewChat: boolean;
  recommendation: DesignRecommendation | null;
  approvedDesignIndex: number | null;
  refinementPlan: RefinementPlan | null;
  deploymentParameters: DeploymentParameters | null;
  designTaskId: string | null;
  designTaskStatus: string | null;
  iac: IaCOutput | null;
  iacTaskId: string | null;
  iacTaskStatus: IaCTaskStatus | null;
  docs: DocumentationOutput | null;
  docsTaskId: string | null;
  docsTaskStatus: DocsTaskStatus | null;
  regeneratingSection: keyof DocumentationOutput | null;
  loading: boolean;
  error: string | null;
  hydrating: boolean;
}

export interface Project {
  tenant_id: string;
  project_id: string;
  name: string;
  mode: "wizard";
  status: string;
  current_step: WizardStep;
  approved_design_index: number | null;
  active_design_task_id: string | null;
  active_iac_task_id: string | null;
  active_docs_task_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectState {
  project: Project;
  requirements: InterviewOutput | null;
  design: DesignRecommendation | null;
  iac: IaCOutput | null;
  docs: DocumentationOutput | null;
}
