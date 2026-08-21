export type AccessLevel = "owner" | "admin" | "publisher" | "user";

export interface CapabilityViewItem {
  capabilityKey: string;
  displayName: string;
  label: string;
}

export interface CapabilityWorkspaceView {
  actorId: string;
  accessLevel: AccessLevel;
  capabilities: readonly CapabilityViewItem[];
}

export const CAPABILITY_EMPTY_STATE =
  "No Domain Capabilities are assigned yet. Ask an organization owner or admin to assign one in Settings → Users.";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function accessLevel(value: unknown): AccessLevel {
  return value === "owner" || value === "admin" || value === "publisher" ? value : "user";
}

export function capabilityWorkspaceView(actorValue: unknown, workspaceValue: unknown): CapabilityWorkspaceView {
  const actor = asRecord(actorValue);
  const workspace = asRecord(workspaceValue);
  const granted = (Array.isArray(workspace.visibleCapabilities) ? workspace.visibleCapabilities : [])
    .map(asRecord)
    .filter((item) => typeof item.capabilityKey === "string");
  const capabilities = granted
    .filter((item, index, all) => (
      all.findIndex((candidate) => candidate.capabilityKey === item.capabilityKey) === index
    ))
    .map((item) => {
      const capabilityKey = String(item.capabilityKey);
      const displayName = String(item.displayName || "Domain Capability");
      return {capabilityKey, displayName, label: `${capabilityKey} · ${displayName}`};
    });

  return {
    actorId: String(actor.actorId || "current user"),
    accessLevel: accessLevel(actor.role),
    capabilities,
  };
}
