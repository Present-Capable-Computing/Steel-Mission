import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import {
  controlPlaneManagementAvailable,
  loadControlPlane,
  saveControlPlaneDocument,
  type ControlPlaneData,
  type ControlPlaneDocument,
} from "./control-plane";
import type {ApiRequester} from "./organizations";


const EMPTY: ControlPlaneData = {integrations: {}, controlPolicy: {}, authPolicy: {}, readiness: {}};

function records(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function parseDocument(value: string, label: string): Record<string, unknown> {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label} must be a JSON object`);
  return parsed as Record<string, unknown>;
}

export function ControlPlanePanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [data, setData] = useState(EMPTY);
  const [controlJson, setControlJson] = useState("{}");
  const [authJson, setAuthJson] = useState("{}");
  const [integrationsJson, setIntegrationsJson] = useState("{}");
  const [status, setStatus] = useState("Loading Control Plane…");

  const setLoaded = (loaded: ControlPlaneData) => {
    setData(loaded);
    setControlJson(JSON.stringify(loaded.controlPolicy, null, 2));
    setAuthJson(JSON.stringify(loaded.authPolicy, null, 2));
    setIntegrationsJson(JSON.stringify(loaded.integrations, null, 2));
    setStatus("");
  };
  const reload = async () => setLoaded(await loadControlPlane(request, accessLevel));

  useEffect(() => {
    let current = true;
    loadControlPlane(request, accessLevel)
      .then((loaded) => current && setLoaded(loaded))
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Control Plane is unavailable"));
    return () => { current = false; };
  }, [accessLevel, request]);

  const save = async (document: ControlPlaneDocument, source: string, label: string) => {
    setStatus(`Saving ${label}…`);
    try {
      await saveControlPlaneDocument(request, accessLevel, document, parseDocument(source, label));
      await reload();
      setStatus(`${label} saved and reloaded.`);
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : `${label} could not be saved`);
    }
  };

  const readinessChecks = records(data.readiness.checks);
  const providers = records(data.integrations.modelProviders);
  const connectors = records(data.integrations.connectors);
  const canManage = controlPlaneManagementAvailable(accessLevel);

  return (
    <div id="controlPlanePanel" class="parity-panel">
      <section>
        <h3>Control Plane Readiness</h3>
        <p>Alpha {String(data.readiness.alphaScore || 0)}% · Production {String(data.readiness.productionScore || 0)}%</p>
        <div class="record-list">
          {readinessChecks.map((check, index) => (
            <article key={String(check.id || index)}><strong>{String(check.label || check.id || "Readiness check")}</strong><p>alpha {check.alpha ? "ready" : "open"} · production {check.production ? "ready" : "open"} · {String(check.detail || "")}</p></article>
          ))}
        </div>
      </section>

      <section>
        <h3>Model Providers</h3>
        <div class="record-list">{providers.map((provider, index) => <article key={String(provider.id || index)}><strong>{String(provider.label || provider.id || "Provider")}</strong><p>{String(provider.id || "")} · {String(provider.status || "registry-ready")}</p></article>)}</div>
      </section>

      <section>
        <h3>Tool Integrations</h3>
        <div class="record-list">{connectors.map((connector, index) => <article key={String(connector.id || index)}><strong>{String(connector.label || connector.id || "Connector")}</strong><p>{String(connector.kind || "integration")} · {String(connector.status || "registry-ready")} · {connector.locked ? "locked" : connector.enabled ? "enabled" : "disabled"}</p></article>)}</div>
      </section>

      {canManage && <section class="panel-form" aria-label="Control Plane configuration">
        <label>Control Policy JSON<textarea rows={16} value={controlJson} onInput={(event) => setControlJson(event.currentTarget.value)} /></label>
        <button type="button" onClick={() => save("control-policy", controlJson, "Control Policy")}>Save Control Policy</button>
        <label>Auth Policy JSON<textarea rows={16} value={authJson} onInput={(event) => setAuthJson(event.currentTarget.value)} /></label>
        <button type="button" onClick={() => save("auth-policy", authJson, "Auth Policy")}>Save Auth Policy</button>
        <label>Integration Registry JSON<textarea rows={16} value={integrationsJson} onInput={(event) => setIntegrationsJson(event.currentTarget.value)} /></label>
        <button type="button" onClick={() => save("integrations", integrationsJson, "Integration Registry")}>Save Integrations</button>
      </section>}
      {status && <p role="status">{status}</p>}
    </div>
  );
}
