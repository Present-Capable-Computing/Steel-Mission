export type SettingsSectionId =
  | "organizations"
  | "people"
  | "missions"
  | "knowledge"
  | "profiles"
  | "integrations"
  | "models"
  | "audit";

export interface SettingsSectionDefinition {
  id: SettingsSectionId;
  label: string;
  hint: string;
}

export const SETTINGS_SECTIONS: readonly SettingsSectionDefinition[] = [
  {id: "organizations", label: "Organizations", hint: "Identity, knowledge bindings, and capability sets"},
  {id: "people", label: "Users", hint: "Create users and capability access"},
  {id: "missions", label: "Missions", hint: "Delivery scope and mission starts"},
  {id: "knowledge", label: "Knowledge", hint: "Knowledge Domains and Domain Capabilities"},
  {id: "profiles", label: "Runtime Profiles", hint: "Snapshot policy and Coordinator model"},
  {id: "integrations", label: "Control Plane", hint: "Policy, evidence, and integrations"},
  {id: "models", label: "Model Roles", hint: "Provider and model-role registry"},
  {id: "audit", label: "Audit", hint: "Configuration mutations"},
];
