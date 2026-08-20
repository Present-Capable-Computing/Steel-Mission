import type {AccessLevel} from "./capabilities";

export interface AssignmentUser {
  id: string;
  name?: string;
  role: string;
  assignedCapabilities: readonly string[];
}

export interface AssignmentCapability {
  capabilityKey: string;
  displayName: string;
}

export interface CapabilityAssignment {
  roleKey: string;
  publishers: string[];
  users: string[];
}

export interface AssignmentRegistry {
  assignments: CapabilityAssignment[];
}

export interface CapabilityOwnership {
  status: "owned" | "unowned";
  label: string;
  action: string;
  userIds: string[];
}

type Requester = (path: string, init?: RequestInit) => Promise<Response>;

export function assignmentControlsAvailable(accessLevel: unknown): boolean {
  return accessLevel === "owner";
}

export function capabilityOwnership(
  capability: AssignmentCapability,
  users: readonly AssignmentUser[],
): CapabilityOwnership {
  const userIds = users
    .filter((user) => user.assignedCapabilities.includes(capability.capabilityKey))
    .map((user) => user.id)
    .sort();
  if (userIds.length === 0) {
    return {
      status: "unowned",
      label: "Unowned",
      action: "Select this capability to assign it to the chosen user.",
      userIds,
    };
  }
  return {
    status: "owned",
    label: "Assigned",
    action: `Assigned to ${userIds.join(", ")}.`,
    userIds,
  };
}

export function assignmentRegistry(
  users: readonly AssignmentUser[],
  capabilities: readonly AssignmentCapability[],
  selectedUserId: string,
  selectedCapabilities: readonly string[],
): AssignmentRegistry {
  const selected = new Set(selectedCapabilities);
  return {
    assignments: capabilities.map((capability) => {
      const assignedUsers = users.filter((user) => {
        const assigned = user.id === selectedUserId ? selected : new Set(user.assignedCapabilities);
        return assigned.has(capability.capabilityKey);
      });
      return {
        roleKey: capability.capabilityKey,
        publishers: assignedUsers.filter((user) => user.role === "publisher").map((user) => user.id),
        users: assignedUsers.filter((user) => user.role === "user").map((user) => user.id),
      };
    }),
  };
}

export async function saveCapabilityAssignment(
  request: Requester,
  users: readonly AssignmentUser[],
  capabilities: readonly AssignmentCapability[],
  selectedUserId: string,
  selectedCapabilities: readonly string[],
  accessLevel: AccessLevel = "owner",
): Promise<AssignmentRegistry> {
  if (!assignmentControlsAvailable(accessLevel)) {
    throw new Error("owner access is required to assign Domain Capabilities");
  }
  const submitted = assignmentRegistry(users, capabilities, selectedUserId, selectedCapabilities);
  const response = await request("/api/owner/assignments", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(submitted),
  });
  const saved = await response.json() as {ok?: boolean; error?: string};
  if (!response.ok || !saved.ok) throw new Error(saved.error || "Capability assignment failed");

  const reloadedResponse = await request("/api/owner/assignments");
  const reloaded = await reloadedResponse.json() as {ok?: boolean; error?: string; assignments?: CapabilityAssignment[]};
  if (!reloadedResponse.ok || !reloaded.ok || !Array.isArray(reloaded.assignments)) {
    throw new Error(reloaded.error || "Capability assignment could not be reloaded");
  }
  return {assignments: reloaded.assignments};
}
