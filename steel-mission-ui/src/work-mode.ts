export type WorkMode = "normal" | "domain-capabilities";

export interface WorkModeDefinition {
  id: WorkMode;
  label: string;
  description: string;
}

export const WORK_MODES: readonly WorkModeDefinition[] = [
  {
    id: "normal",
    label: "Normal",
    description: "Normal chat uses direct prompts and answers while keeping assigned capabilities available as context.",
  },
  {
    id: "domain-capabilities",
    label: "Domain Capabilities",
    description: "Domain Capabilities uses capability-focused prompts and answers through the assigned role and governed knowledge lens.",
  },
];

export function isWorkMode(value: string): value is WorkMode {
  return WORK_MODES.some((mode) => mode.id === value);
}
