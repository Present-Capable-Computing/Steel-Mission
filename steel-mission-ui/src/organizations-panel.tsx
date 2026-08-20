import {useEffect, useState} from "preact/hooks";

import type {AccessLevel} from "./capabilities";
import {
  loadOrganizationRegistry,
  organizationPanelAvailable,
  saveOrganizationRegistry,
  updateOrganization,
  type ApiRequester,
  type Organization,
  type OrganizationRegistry,
} from "./organizations";


function stringList(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

function jsonList(value: string, label: string): unknown[] {
  const parsed = JSON.parse(value || "[]") as unknown;
  if (!Array.isArray(parsed)) throw new Error(`${label} must be a JSON array`);
  return parsed;
}

function template(): Organization {
  return {
    id: "",
    name: "",
    slug: "",
    identifiers: {legalName: "", domain: "", country: "", environment: "starter", dataClassification: "internal"},
    knowledgeDomainKeys: [],
    domainCapabilityKeys: [],
    knowledgeSources: {repositories: [], documents: []},
    notes: "",
  };
}

function slug(value: string): string {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export function OrganizationsPanel({accessLevel, request}: {accessLevel: AccessLevel; request: ApiRequester}) {
  const [registry, setRegistry] = useState<OrganizationRegistry | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<Organization>(template());
  const [status, setStatus] = useState("Loading organizations…");

  useEffect(() => {
    let current = true;
    if (!organizationPanelAvailable(accessLevel)) {
      setStatus("Owner or admin access is required to manage organizations.");
      return () => { current = false; };
    }
    loadOrganizationRegistry(request, accessLevel)
      .then((loaded) => {
        if (!current) return;
        const initial = loaded.organizations.find((item) => item.id === loaded.activeOrganizationId) || loaded.organizations[0] || template();
        setRegistry(loaded);
        setSelectedId(initial.id);
        setDraft(initial);
        setStatus("");
      })
      .catch((error: unknown) => current && setStatus(error instanceof Error ? error.message : "Organizations are unavailable"));
    return () => { current = false; };
  }, [accessLevel, request]);

  if (!organizationPanelAvailable(accessLevel)) {
    return <div id="organizationsPanel"><p>{status}</p></div>;
  }

  const selectOrganization = (id: string) => {
    const selected = registry?.organizations.find((item) => item.id === id) || template();
    setSelectedId(id);
    setDraft(selected);
    setStatus("");
  };
  const setField = (key: keyof Organization, value: unknown) => setDraft((current) => ({...current, [key]: value}));
  const setIdentifier = (key: string, value: string) => setDraft((current) => ({
    ...current,
    identifiers: {...current.identifiers, [key]: value},
  }));
  const save = async (event: Event) => {
    event.preventDefault();
    if (!registry) return;
    setStatus("Saving organization…");
    try {
      const next = updateOrganization(registry, {
        ...draft,
        id: slug(draft.id || draft.name),
        name: draft.name.trim(),
        slug: slug(draft.slug || draft.name),
        knowledgeDomainKeys: stringList((draft.knowledgeDomainKeys || []).join(", ")),
        domainCapabilityKeys: stringList((draft.domainCapabilityKeys || []).join(", ")),
        knowledgeSources: {
          repositories: jsonList(JSON.stringify(draft.knowledgeSources?.repositories || []), "Repositories"),
          documents: jsonList(JSON.stringify(draft.knowledgeSources?.documents || []), "Documents"),
        },
      });
      const saved = await saveOrganizationRegistry(request, accessLevel, next);
      setRegistry(saved);
      setSelectedId(draft.id);
      setStatus("Organization saved and reloaded.");
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : "Organization could not be saved");
    }
  };
  const identifiers = draft.identifiers || {};
  const sources = draft.knowledgeSources || {};

  return (
    <div id="organizationsPanel" class="parity-panel">
      <div class="panel-actions">
        <label>Organization
          <select value={selectedId} onChange={(event) => selectOrganization(event.currentTarget.value)}>
            {registry?.organizations.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
          </select>
        </label>
        <button type="button" onClick={() => { setSelectedId(""); setDraft(template()); }}>Create Organization</button>
      </div>
      <form class="panel-form" onSubmit={save}>
        <label>Active Organization
          <select
            value={registry?.activeOrganizationId || ""}
            onChange={(event) => registry && setRegistry({...registry, activeOrganizationId: event.currentTarget.value})}
          >
            {registry?.organizations.map((item) => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}
          </select>
        </label>
        <div class="panel-grid">
          <label>Organization ID<input value={draft.id} readOnly={Boolean(selectedId)} onInput={(event) => setField("id", event.currentTarget.value)} /></label>
          <label>Display Name<input value={draft.name} onInput={(event) => {
            const name = event.currentTarget.value;
            setDraft((current) => ({...current, name, ...(!selectedId ? {id: slug(name), slug: slug(name)} : {})}));
          }} /></label>
          <label>Slug<input value={draft.slug} onInput={(event) => setField("slug", event.currentTarget.value)} /></label>
          <label>Legal Name<input value={String(identifiers.legalName || "")} onInput={(event) => setIdentifier("legalName", event.currentTarget.value)} /></label>
          <label>Primary Domain<input value={String(identifiers.domain || "")} onInput={(event) => setIdentifier("domain", event.currentTarget.value)} /></label>
          <label>Country<input value={String(identifiers.country || "")} onInput={(event) => setIdentifier("country", event.currentTarget.value)} /></label>
          <label>Environment<input value={String(identifiers.environment || "")} onInput={(event) => setIdentifier("environment", event.currentTarget.value)} /></label>
          <label>Data Classification<input value={String(identifiers.dataClassification || "")} onInput={(event) => setIdentifier("dataClassification", event.currentTarget.value)} /></label>
        </div>
        <label>Knowledge Domain Keys<input value={(draft.knowledgeDomainKeys || []).join(", ")} onInput={(event) => setField("knowledgeDomainKeys", stringList(event.currentTarget.value))} /></label>
        <label>Domain Capability Keys<input value={(draft.domainCapabilityKeys || []).join(", ")} onInput={(event) => setField("domainCapabilityKeys", stringList(event.currentTarget.value))} /></label>
        <label>Organization Repositories and Folders JSON<textarea rows={5} value={JSON.stringify(sources.repositories || [], null, 2)} onInput={(event) => {
          try { setDraft((current) => ({...current, knowledgeSources: {...current.knowledgeSources, repositories: jsonList(event.currentTarget.value, "Repositories")}})); } catch { /* validate on save */ }
        }} /></label>
        <label>Organization Documents JSON<textarea rows={5} value={JSON.stringify(sources.documents || [], null, 2)} onInput={(event) => {
          try { setDraft((current) => ({...current, knowledgeSources: {...current.knowledgeSources, documents: jsonList(event.currentTarget.value, "Documents")}})); } catch { /* validate on save */ }
        }} /></label>
        <label>Notes<textarea rows={3} value={draft.notes || ""} onInput={(event) => setField("notes", event.currentTarget.value)} /></label>
        <button type="submit" disabled={!registry || !draft.name.trim()}>Save Organization</button>
        {status && <p role="status">{status}</p>}
      </form>
    </div>
  );
}
