import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import {
  knowledgeManagementAvailable,
  loadKnowledgeRegistry,
  previewPreparedSnapshot,
  saveKnowledgeSources,
  uploadKnowledge,
  type KnowledgeRegistry,
} from "./knowledge";
import {startMission} from "./missions";
import type {ApiRequester} from "./organizations";


function records(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function parseList(value: string, label: string): unknown[] {
  const parsed = JSON.parse(value || "[]") as unknown;
  if (!Array.isArray(parsed)) throw new Error(`${label} must be a JSON array`);
  return parsed;
}

async function uploadRecords(files: File[]): Promise<Array<Record<string, unknown>>> {
  return Promise.all(files.slice(0, 200).map(async (file) => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      binary += String.fromCharCode(...bytes.slice(offset, offset + 8192));
    }
    return {
      name: file.name,
      relativePath: file.webkitRelativePath || file.name,
      type: file.type || "",
      size: file.size,
      contentBase64: btoa(binary),
    };
  }));
}

export function KnowledgePanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [registry, setRegistry] = useState<KnowledgeRegistry | null>(null);
  const [repositoriesJson, setRepositoriesJson] = useState("[]");
  const [documentsJson, setDocumentsJson] = useState("[]");
  const [uploadLabel, setUploadLabel] = useState("organization-knowledge");
  const [files, setFiles] = useState<File[]>([]);
  const [status, setStatus] = useState("Loading knowledge…");

  const setLoaded = (loaded: KnowledgeRegistry) => {
    setRegistry(loaded);
    setRepositoriesJson(JSON.stringify(loaded.generalKnowledge?.repositories || [], null, 2));
    setDocumentsJson(JSON.stringify(loaded.generalKnowledge?.documents || [], null, 2));
    setStatus("");
  };
  const reload = async () => setLoaded(await loadKnowledgeRegistry(request, accessLevel));

  useEffect(() => {
    let current = true;
    loadKnowledgeRegistry(request, accessLevel)
      .then((loaded) => current && setLoaded(loaded))
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Knowledge is unavailable"));
    return () => { current = false; };
  }, [accessLevel, request]);

  const save = async () => {
    setStatus("Saving knowledge sources…");
    try {
      await saveKnowledgeSources(request, accessLevel, {
        repositories: parseList(repositoriesJson, "Repositories"),
        documents: parseList(documentsJson, "Documents"),
      });
      await reload();
      setStatus("Knowledge sources saved and reloaded.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Knowledge sources could not be saved");
    }
  };
  const preview = async () => {
    setStatus("Preparing snapshot preview…");
    try {
      const payload = await previewPreparedSnapshot(request, accessLevel);
      const quality = payload.knowledgeQuality && typeof payload.knowledgeQuality === "object" ? payload.knowledgeQuality as Record<string, unknown> : {};
      setStatus(`Prepared snapshot: ${payload.availableSourceCount || 0}/${payload.sourceCount || 0} sources available · quality ${quality.status || "unknown"}.`);
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Prepared snapshot is unavailable");
    }
  };
  const prepare = async () => {
    setStatus("Starting snapshot preparation…");
    try {
      const result = await startMission(request, {
        templateId: "prepare-knowledge",
        objective: "Prepare the organization knowledge snapshot from the current source registry.",
      });
      setStatus(`Snapshot preparation mission ${result.missionId || "started"}.`);
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Snapshot mission could not be started");
    }
  };
  const upload = async () => {
    if (files.length === 0) { setStatus("Choose files first."); return; }
    setStatus("Uploading knowledge…");
    try {
      const payload = await uploadKnowledge(request, accessLevel, {
        label: uploadLabel,
        sourceKind: files.some((file) => Boolean(file.webkitRelativePath)) ? "folder" : "files",
        organizationId: String(registry?.activeOrganization?.id || ""),
        files: await uploadRecords(files),
      });
      await reload();
      const mission = payload.mission && typeof payload.mission === "object" ? payload.mission as Record<string, unknown> : {};
      setStatus(`Knowledge uploaded.${mission.missionId ? ` Mission ${mission.missionId} started.` : ""}`);
      setFiles([]);
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Knowledge upload failed");
    }
  };

  const capabilities = records(registry?.capabilities || registry?.roles);
  const domains = records(registry?.knowledgeDomains || registry?.foundations);
  const effective = registry?.effectiveKnowledge || registry?.generalKnowledge || {repositories: [], documents: []};
  const quality = registry?.knowledgeQuality || {};
  const issues = records(quality.issues);
  const canManage = knowledgeManagementAvailable(accessLevel);

  return (
    <div id="knowledgePanel" class="parity-panel">
      <section>
        <h3>Knowledge Quality</h3>
        <p>{String(quality.status || "unknown")} · context sufficient: {quality.contextSufficient ? "yes" : "no"} · {String(quality.staleSourceCount || 0)} stale · {String(quality.unownedSourceCount || 0)} unowned</p>
        <div class="record-list">
          {issues.slice(0, 8).map((issue, index) => <article key={String(issue.id || index)}><strong>{String(issue.severity || "warning")} · {String(issue.id || "knowledge-warning")}</strong><p>{String(issue.message || "")}</p></article>)}
        </div>
      </section>

      {canManage && (
        <section class="panel-form" aria-label="Shared Knowledge Source Pool">
          <h3>Shared Knowledge Source Pool</h3>
          <label>Repositories and Folders JSON<textarea rows={8} value={repositoriesJson} onInput={(event) => setRepositoriesJson(event.currentTarget.value)} /></label>
          <label>Documents JSON<textarea rows={8} value={documentsJson} onInput={(event) => setDocumentsJson(event.currentTarget.value)} /></label>
          <div class="panel-actions">
            <button type="button" onClick={save}>Save Sources</button>
            <button type="button" onClick={preview}>Preview Prepared Snapshot</button>
            <button type="button" onClick={prepare}>Prepare Snapshot</button>
          </div>
          <div class="panel-grid">
            <label>Upload Label<input value={uploadLabel} onInput={(event) => setUploadLabel(event.currentTarget.value)} /></label>
            <label>Files<input type="file" multiple onChange={(event) => setFiles(Array.from(event.currentTarget.files || []))} /></label>
          </div>
          <button type="button" disabled={files.length === 0} onClick={upload}>Add Files and Prepare Snapshot</button>
        </section>
      )}

      <section>
        <h3>Effective Source Registry</h3>
        <p>{(effective.repositories || []).length} repositories/folders · {(effective.documents || []).length} documents</p>
      </section>
      <section>
        <h3>Knowledge Domains</h3>
        <div class="record-list">{domains.map((domain, index) => <article key={String(domain.domainKey || domain.role_key || index)}><strong>{String(domain.domainKey || domain.fNumber || domain.role_key || "") } · {String(domain.displayName || domain.display_name || "")}</strong><p>{String(domain.canonPath || domain.canon_path || "")}</p></article>)}</div>
      </section>
      <section>
        <h3>Domain Capabilities</h3>
        <div class="record-list">{capabilities.map((capability, index) => <article key={String(capability.capabilityKey || capability.roleKey || index)}><strong>{String(capability.capabilityKey || capability.roleKey || "")} · {String(capability.displayName || "")}</strong><p>{String(capability.canonPath || "")}</p></article>)}</div>
      </section>
      {status && <p role="status">{status}</p>}
    </div>
  );
}
